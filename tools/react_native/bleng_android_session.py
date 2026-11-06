"""Concrete, one-shot Android automation lifecycle for the formal scan host.

The supported prepared profile has default hidden-API keys, package-scope mock
AppOps already allowed, and a pre-existing mock-app store containing Settings.
Stock Appium may temporarily set the policy keys to 1; successful DELETE must
return them to the original absent/default state. This owner only observes that
bounded profile: no install, root/grant, settings repair or unknown-process stop.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only
from urllib.parse import urlsplit

from tools.android.android_appium import prepared_driver_capabilities
from tools.android.automation_observation import (
    parse_active_instrumentation,
    require_no_settings_services,
)
from tools.android.state_control import AndroidRadioController
from tools.mobile_appium import read_loopback_json, require_attached_driver

from . import android_sdk
from .bleng_android_automation import (
    HELPER_PACKAGES,
    HIDDEN_API_POLICIES,
    INSTRUMENTATION_COMPONENT,
    MAX_OUTPUT_BYTES,
    SERVER_PACKAGE,
    SETTINGS_PACKAGE,
    _collect_prepared_android_automation,
    parse_adb_forward_list,
    parse_mock_location_scopes,
)
from .capture_io import publish_new_json

# The physical OnePlus/API-36 global AMS dump is about 1.4 MiB. Keep ordinary
# reads at 1 MiB; only this exact diagnostic gets a separate exclusive ceiling.
MAX_PROCESSES_OUTPUT_BYTES = 4 * MAX_OUTPUT_BYTES


class AndroidAutomationOwner:
    """Own one prepared → requesting → bound → deleted → finalized scope.

    No external report can construct authority. prepare/begin collect live data
    themselves; bound host/device session IDs are distinct. require_owner is a
    cheap local guard, not another SDK sweep per UI field. Finalization uses an
    independent budget, verifies idle observations and never releases the phone.
    The trusted host serializes method calls; this is not a cross-thread arbiter.
    These are cooperative, bracketed observations, not atomic OS attestation.
    """

    def __init__(
        self,
        *,
        phone: AndroidRadioController,
        run_id: str,
        app_id: str,
        appium_home: Path,
        expected_apk_sha256: dict[str, str],
        system_port: int,
        output: Path,
        cwd: Path,
        deadline: float,
    ) -> None:
        if type(phone) is not AndroidRadioController or phone.run_id != run_id:
            raise ValueError("same-run actual Android phone lease required")
        if (
            type(app_id) is not str
            or re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", app_id
            )
            is None
        ):
            raise ValueError("explicit canonical Android App package required")
        if type(system_port) is not int or not 1 <= system_port <= 65535:
            raise ValueError("explicit Android automation system port required")
        if (
            type(expected_apk_sha256) is not dict
            or set(expected_apk_sha256) != set(HELPER_PACKAGES)
            or any(
                type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in expected_apk_sha256.values()
            )
        ):
            raise ValueError("exactly three reviewed helper SHA256 values required")
        if not all(isinstance(path, Path) for path in (appium_home, output, cwd)):
            raise TypeError("explicit local Android automation paths required")
        self._remaining(deadline)
        self._phone, self._run_id, self._device = phone, run_id, phone.device
        self._app_id, self._appium_home, self._system_port = (
            app_id,
            appium_home.absolute(),
            system_port,
        )
        self._expected, self._output, self._cwd = (
            dict(expected_apk_sha256),
            output.absolute(),
            cwd,
        )
        self._deadline = deadline
        self._phase = "new"
        self._failure: BaseException | None = None
        self._close_failure: BaseException | None = None
        self._finalized = False
        self._records: dict[Path, str] = {}
        self._baseline: dict | None = None
        self._extra: dict | None = None
        self._driver = self._executor = self._config = None
        self._server_url: str | None = None
        self._host_session_id: str | None = None
        self._device_session_id: str | None = None
        self._server_identity: android_sdk.AndroidAppIdentity | None = None
        self._settings_identity: android_sdk.AndroidAppIdentity | None = None
        self._server_logs_attempted = False
        self._server_status_attempted = False
        self._adb_transport_attempted = False
        self._bound_server_selector: tuple | None = None
        self._sequence = 0
        self._phone_guard(deadline)
        self._output.mkdir(mode=0o700)

    @property
    def system_port(self) -> int:
        return self._system_port

    @property
    def appium_home(self) -> Path:
        return self._appium_home

    @staticmethod
    def _remaining(deadline: float) -> float:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite absolute Android automation deadline required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Android automation owner deadline exceeded")
        return remaining

    def _phone_guard(self, deadline: float) -> None:
        self._remaining(deadline)
        phone = self._phone
        if (
            phone.device != self._device
            or phone.run_id != self._run_id
            or phone.document.get("device") != self._device
            or phone.document.get("run_id") != self._run_id
            or phone.document.get("actions") != []
            or phone.initial_state is not None
            or phone.last_state is not None
        ):
            raise RuntimeError("Android automation phone/run ownership changed")
        phone.require_owner()
        self._remaining(deadline)

    @staticmethod
    def _record_digest(path: Path) -> str:
        before = path.lstat()
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size > 8 * MAX_OUTPUT_BYTES
            ):
                raise RuntimeError(
                    "Android automation evidence is linked/nonregular/oversized"
                )
            raw = stream.read(8 * MAX_OUTPUT_BYTES + 1)
            finished = os.fstat(stream.fileno())
        after = path.lstat()
        signatures = {
            (
                v.st_dev,
                v.st_ino,
                v.st_mode,
                v.st_nlink,
                v.st_size,
                v.st_mtime_ns,
                v.st_ctime_ns,
            )
            for v in (before, opened, finished, after)
        }
        if len(signatures) != 1 or len(raw) != before.st_size:
            raise RuntimeError("Android automation evidence changed while reading")
        return hashlib.sha256(raw).hexdigest()

    def _records_current(self) -> None:
        for path, expected in self._records.items():
            if self._record_digest(path) != expected:
                raise RuntimeError("Android automation evidence changed")

    def _guard(self, deadline: float, *, records: bool = False) -> None:
        if self._failure is not None:
            raise RuntimeError(
                "Android automation authority invalidated"
            ) from self._failure
        try:
            self._phone_guard(deadline)
            if records:
                self._records_current()
            if self._driver is not None:
                self._driver_current()
            self._phone_guard(deadline)
        except TimeoutError:
            raise
        except Exception as error:
            self._failure = error
            raise

    def require_owner(self, deadline: float) -> None:
        if self._finalized or self._phase not in {"prepared", "bound", "deleted"}:
            raise RuntimeError("Android automation owner is not in a usable phase")
        self._guard(deadline)

    def _publish(
        self, name: str, value: dict, *, deadline: float | None = None
    ) -> None:
        path = self._output / name
        published = []
        digest = publish_new_json(path, value, on_commit=published.append)
        self._records[path] = digest
        if deadline is None:
            return
        try:
            self._guard(deadline, records=True)
        except BaseException as error:
            try:
                info = path.lstat()
                if (
                    published != [(info.st_dev, info.st_ino)]
                    or self._record_digest(path) != digest
                ):
                    raise RuntimeError(
                        "automation receipt replaced; refusing retraction"
                    )
                current = path.lstat()
                if (current.st_dev, current.st_ino) != published[0]:
                    raise RuntimeError(
                        "automation receipt identity changed before retraction"
                    )
                path.unlink()
                self._records.pop(path)
            except BaseException as retraction:  # noqa: BLE001 -- authority/deadline failure remains primary.
                error.add_note(f"Android terminal receipt retraction: {retraction}")
            raise

    def _command(self, deadline: float, *args: str) -> str:
        self._guard(deadline, records=True)
        argv = android_sdk.adb_command(self._device, *args)
        self._guard(deadline)
        self._sequence += 1
        receipt = {"argv": argv, "stdout": "", "returncode": None, "error": None}
        primary: BaseException | None = None
        try:
            result = android_sdk.run_command(
                argv,
                cwd=self._cwd,
                timeout=min(5.0, self._remaining(deadline)),
                check=False,
            )
            receipt["returncode"] = result.returncode
            if type(result.stdout) is not str:
                raise android_sdk.AndroidSdkError(
                    "invalid Android automation SDK readback"
                )
            raw = result.stdout.encode()
            is_processes = args == ("shell", "dumpsys", "activity", "processes")
            limit = MAX_PROCESSES_OUTPUT_BYTES if is_processes else MAX_OUTPUT_BYTES
            if args == ("shell", "settings", "list", "global"):
                # The full key inventory can contain unrelated device identifiers.
                # Keep its digest and only the three policy rows we actually use.
                receipt["stdout_sha256"] = hashlib.sha256(raw).hexdigest()
                receipt["stdout_scope"] = "hidden-API keys only"
                receipt["stdout"] = "\n".join(
                    line
                    for line in result.stdout.splitlines()
                    if line.partition("=")[0] in HIDDEN_API_POLICIES
                )[:MAX_OUTPUT_BYTES]
            else:
                receipt["stdout"] = raw[:limit].decode(errors="replace")
            too_large = len(raw) >= limit if is_processes else len(raw) > limit
            if result.returncode or too_large:
                raise android_sdk.AndroidSdkError(
                    f"Android automation read failed (exit={result.returncode}): {str(receipt['stdout'])[-1500:]}"
                )
        except BaseException as error:  # noqa: BLE001 -- retain SDK errors and interruptions with their receipts.
            primary = error
        try:
            self._guard(deadline)
        except BaseException as boundary:  # noqa: BLE001 -- primary SDK errors take precedence over guard errors.
            if primary is None:
                primary = boundary
            else:
                primary.add_note(f"Android post-command boundary: {boundary}")
        if primary is not None:
            receipt["error"] = str(primary)
        try:
            self._publish(f"{self._sequence:03d}-read.json", receipt)
        except BaseException as publication:
            if primary is None:
                raise
            primary.add_note(f"Android automation receipt publication: {publication}")
        if primary is not None:
            raise primary
        return result.stdout

    def _inventory(self, name: str, deadline: float) -> dict:
        self._guard(deadline, records=True)
        path, observations = _collect_prepared_android_automation(
            phone=self._phone,
            appium_home=self._appium_home,
            expected_apk_sha256=self._expected,
            system_port=self._system_port,
            output=self._output / name,
            cwd=self._cwd,
            deadline=deadline,
        )
        # Only files created by this in-process live collection are retained.
        # No API accepts a ready JSON path supplied by a caller.
        for record in path.parent.glob("*.json"):
            self._records[record] = self._record_digest(record)
        return observations

    def _extra_observations(self, deadline: float, *, active: bool) -> dict:
        instruments = parse_active_instrumentation(
            self._command(deadline, "shell", "dumpsys", "activity", "processes")
        )
        if active:
            identity = self._server_identity
            expected = [
                {
                    "component": INSTRUMENTATION_COMPONENT,
                    "finished": False,
                    "pid": identity.pid,
                    "process_name": SERVER_PACKAGE,
                    "uid": identity.uid,
                    "target_package": SERVER_PACKAGE,
                }
            ]
            if instruments != expected:
                raise RuntimeError(
                    "active instrumentation differs from the bound server identity"
                )
        elif instruments:
            raise RuntimeError(
                "unknown pre-existing or residual active instrumentation"
            )
        require_no_settings_services(
            self._command(
                deadline, "shell", "dumpsys", "activity", "services", SETTINGS_PACKAGE
            )
        )
        store = self._command(
            deadline, "shell", "cat", "/data/local/tmp/mock_apps.json"
        )
        packages = json.loads(store)
        if (
            type(packages) is not list
            or not packages
            or any(
                type(p) is not str
                or re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", p) is None
                for p in packages
            )
            or len(set(packages)) != len(packages)
            or SETTINGS_PACKAGE not in packages
        ):
            raise RuntimeError("explicitly prepared Settings mock-app store required")
        global_settings = self._command(deadline, "shell", "settings", "list", "global")
        if not global_settings.endswith("\n"):
            raise RuntimeError("incomplete global settings key inventory")
        hidden = {}
        seen = set()
        for line in global_settings.splitlines():
            key, separator, value = line.partition("=")
            if not separator or not key or key in seen:
                raise RuntimeError("unsupported global settings key inventory")
            seen.add(key)
            if key in HIDDEN_API_POLICIES:
                hidden[key] = value
        expected_hidden = {key: "1" for key in HIDDEN_API_POLICIES} if active else {}
        if hidden != expected_hidden:
            raise RuntimeError(
                "hidden-API key existence/value differs from the supported lifecycle profile"
            )
        ime = {}
        for key in ("default_input_method", "enabled_input_methods"):
            value = self._command(deadline, "shell", "settings", "get", "secure", key)
            if not value.strip() or "\r" in value or len(value.splitlines()) != 1:
                raise RuntimeError("unsupported IME baseline readback")
            ime[key] = value
        return {"mock_app_store": store, "ime": ime}

    def _scenario(self, deadline: float) -> None:
        self._remaining(deadline)
        if deadline > self._deadline:
            raise ValueError(
                "Android session operation cannot extend the scenario deadline"
            )

    def _idle(self, name: str, deadline: float) -> tuple[dict, dict]:
        data = self._inventory(name, deadline)
        if set(data["observed_hidden_api_policy"].values()) != {"null"}:
            raise RuntimeError(
                "prepared profile requires absent/default hidden-API keys"
            )
        scopes = data["observed_mock_location_scopes"]
        if scopes.get("package") != "allow" or set(scopes.values()) != {"allow"}:
            raise RuntimeError(
                "explicitly prepared package-scope Settings mock-location allow required"
            )
        return data, self._extra_observations(deadline, active=False)

    def _same_baseline(self, data: dict, extra: dict) -> None:
        for key in (
            "boot_id",
            "helpers",
            "settings_processes",
            "forwardings",
            "observed_hidden_api_policy",
            "observed_mock_location_scopes",
        ):
            if data[key] != self._baseline[key]:
                raise RuntimeError(f"Android automation baseline changed: {key}")
        if extra != self._extra:
            raise RuntimeError("Android Settings store/IME baseline changed")

    def prepare(self) -> None:
        if self._finalized or self._phase != "new":
            raise RuntimeError("Android automation prepare is one-shot")
        self._phase = "preparing"
        try:
            data, extra = self._idle("prepared", self._deadline)
            if data["settings_processes"]:
                self._settings_identity = android_sdk.read_app_identity(
                    serial=self._device,
                    package=SETTINGS_PACKAGE,
                    expected_apk_sha256=self._expected[SETTINGS_PACKAGE],
                    expected_identity=None,
                    cwd=self._cwd,
                    deadline=self._deadline,
                    require_owner=lambda: self._guard(self._deadline, records=True),
                )
                identity = self._settings_identity
                process = data["settings_processes"][0]
                if (
                    identity.boot_id != data["boot_id"]
                    or identity.pid != process["pid"]
                    or identity.uid != process["uid"]
                    or identity.cmdline != SETTINGS_PACKAGE
                    or identity.base_apk_path
                    != data["helpers"][SETTINGS_PACKAGE]["installed_path"]
                ):
                    raise RuntimeError(
                        "cached Settings process differs from prepared OS/install identity"
                    )
            self._baseline, self._extra = data, extra
            self._publish(
                "prepared-owner.json",
                {
                    "run_id": self._run_id,
                    "app_id": self._app_id,
                    "system_port": self._system_port,
                    "status": "prepared",
                    "extra": extra,
                },
                deadline=self._deadline,
            )
            self._guard(self._deadline, records=True)
            self._phase = "prepared"
        except BaseException as error:
            self._failure = error
            raise

    def require_prepared(
        self, *, phone, run_id: str, app_id: str, deadline: float
    ) -> None:
        self._scenario(deadline)
        if (
            self._finalized
            or self._phase != "prepared"
            or phone is not self._phone
            or run_id != self._run_id
            or app_id != self._app_id
        ):
            raise RuntimeError(
                "Android prepared owner differs from phone/run/App handoff"
            )
        self._guard(deadline, records=True)

    def begin_session(self, *, server_url: str, deadline: float) -> None:
        if type(server_url) is not str:
            raise ValueError("explicit owned Appium endpoint required")
        url = urlsplit(server_url)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.port is None
            or not 1 <= url.port <= 65535
            or url.port == self._system_port
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(char.isspace() for char in server_url)
        ):
            raise ValueError("explicit owned loopback Appium server endpoint required")
        self.require_prepared(
            phone=self._phone,
            run_id=self._run_id,
            app_id=self._app_id,
            deadline=deadline,
        )
        self._phase = "admitting"
        try:
            data, extra = self._idle("admission", deadline)
            self._same_baseline(data, extra)
            self._server_url = server_url
            self._phase = "requesting"
        except BaseException as error:
            self._failure = error
            raise

    def _driver_current(self) -> None:
        driver = self._driver
        require_attached_driver(
            driver,
            session_id=self._host_session_id,
            platform="android",
            device=self._device,
            app_id=self._app_id,
        )
        if (
            driver.command_executor is not self._executor
            or self._executor.client_config is not self._config
        ):
            raise RuntimeError("Android driver transport changed")
        if getattr(self._config, "remote_server_addr", None) != self._server_url:
            raise RuntimeError("Android driver Appium endpoint changed")
        timeout = self._config.timeout
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise RuntimeError("Android driver requires a bounded HTTP timeout")
        for key, expected in prepared_driver_capabilities(self._system_port).items():
            values = [
                driver.capabilities[k]
                for k in (key, "appium:" + key)
                if k in driver.capabilities
            ]
            if not values or any(
                type(value) is not type(expected) or value != expected
                for value in values
            ):
                raise RuntimeError(f"Android prepared driver capability changed: {key}")
        for key in (
            "app",
            "otherApps",
            "uninstallOtherPackages",
            "gpsEnabled",
            "hideKeyboard",
            "unicodeKeyboard",
            "language",
            "locale",
            "localeScript",
            "timeZone",
            "orientation",
            "mjpegServerPort",
            "autoWebview",
        ):
            if key in driver.capabilities or "appium:" + key in driver.capabilities:
                raise RuntimeError(
                    f"unapproved prepared driver side-effect capability: {key}"
                )

    def _identity(self, deadline: float) -> android_sdk.AndroidAppIdentity:
        identity = android_sdk.read_app_identity(
            serial=self._device,
            package=SERVER_PACKAGE,
            expected_apk_sha256=self._expected[SERVER_PACKAGE],
            expected_identity=self._server_identity,
            cwd=self._cwd,
            deadline=deadline,
            require_owner=lambda: self._guard(deadline, records=True),
        )
        baseline = self._baseline
        if (
            identity.boot_id != baseline["boot_id"]
            or identity.uid != baseline["helpers"][SERVER_PACKAGE]["uid"]
            or identity.base_apk_path
            != baseline["helpers"][SERVER_PACKAGE]["installed_path"]
        ):
            raise RuntimeError(
                "server identity differs from prepared helper installation"
            )
        return identity

    def _active_processes(self, deadline: float) -> None:
        lines = self._command(
            deadline, "shell", "ps", "-A", "-o", "PID,UID,ARGS"
        ).splitlines()
        if not lines or lines[0].split() != ["PID", "UID", "ARGS"]:
            raise RuntimeError("unsupported active process inventory header")
        expected = [
            {
                "pid": self._server_identity.pid,
                "uid": self._server_identity.uid,
                "args": SERVER_PACKAGE,
            }
        ] + self._baseline["settings_processes"]
        uids = {helper["uid"] for helper in self._baseline["helpers"].values()}
        related, seen = [], set()
        for line in lines[1:]:
            fields = line.split(maxsplit=2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise RuntimeError("malformed active process inventory")
            pid, uid, args = int(fields[0]), int(fields[1]), fields[2]
            if not 0 < pid <= 2**31 - 1 or pid in seen:
                raise RuntimeError("duplicate/invalid active process identity")
            seen.add(pid)
            name = args.split()[0]
            if (
                name.rsplit("/", 1)[-1] == "uiautomator"
                or "com.android.commands.uiautomator.Launcher" in args.split()
            ):
                raise RuntimeError("unowned standalone uiautomator process")
            if uid in uids or any(
                name == p or name.startswith(p + ":") for p in HELPER_PACKAGES
            ):
                related.append({"pid": pid, "uid": uid, "args": args})
        if sorted(related, key=lambda p: p["pid"]) != sorted(
            expected, key=lambda p: p["pid"]
        ):
            raise RuntimeError(
                "active helper process scope differs from the prepared/bound identities"
            )
        if self._settings_identity is not None:
            android_sdk.read_app_identity(
                serial=self._device,
                package=SETTINGS_PACKAGE,
                expected_apk_sha256=self._expected[SETTINGS_PACKAGE],
                expected_identity=self._settings_identity,
                cwd=self._cwd,
                deadline=deadline,
                require_owner=lambda: self._guard(deadline, records=True),
            )

    def _active_observations(self, deadline: float) -> str:
        self._active_processes(deadline)
        extra = self._extra_observations(deadline, active=True)
        if extra != self._extra:
            raise RuntimeError("Android Settings store/IME changed while binding")
        for key in HIDDEN_API_POLICIES:
            if (
                self._command(
                    deadline, "shell", "settings", "get", "global", key
                ).strip()
                != "1"
            ):
                raise RuntimeError("unexpected active hidden-API policy")
        value = self._command(
            deadline,
            "shell",
            "cmd",
            "appops",
            "get",
            SETTINGS_PACKAGE,
            "android:mock_location",
        )
        scopes = parse_mock_location_scopes(value)
        if scopes != self._baseline["observed_mock_location_scopes"]:
            raise RuntimeError("Settings mock-location scope changed")
        expected_forwards = sorted(
            self._baseline["forwardings"]
            + [[self._device, f"tcp:{self._system_port}", "tcp:6790"]]
        )
        rows = parse_adb_forward_list(self._command(deadline, "forward", "--list"))
        if rows != expected_forwards:
            raise RuntimeError(
                "automation forward identity or unrelated baseline changed"
            )
        self._guard(deadline)
        response = read_loopback_json(
            f"http://127.0.0.1:{self._system_port}", "/sessions", deadline=deadline
        )
        self._guard(deadline)
        sessions = response.get("value")
        if (
            type(sessions) is not list
            or len(sessions) != 1
            or type(sessions[0]) is not dict
        ):
            raise RuntimeError("expected one downstream device session")
        session = sessions[0].get("id")
        if type(session) is not str or str(uuid.UUID(session)) != session:
            raise RuntimeError("invalid downstream device session identity")
        if self._device_session_id is not None and session != self._device_session_id:
            raise RuntimeError(
                "downstream device session changed across the host proxy request"
            )
        return session

    def bind_session(self, driver, *, deadline: float) -> None:
        self._scenario(deadline)
        if self._finalized or self._phase != "requesting":
            raise RuntimeError("Android session binding is one-shot after submission")
        self._phase = "binding"
        try:
            self._driver, self._host_session_id = driver, driver.session_id
            self._executor, self._config = (
                driver.command_executor,
                driver.command_executor.client_config,
            )
            self._guard(deadline, records=True)
            self._server_identity = self._identity(deadline)
            self._device_session_id = self._active_observations(deadline)
            original_timeout = self._config.timeout
            try:
                self._config.timeout = min(
                    original_timeout, 5.0, self._remaining(deadline)
                )
                settings = (
                    driver.get_settings()
                )  # Actual host→device proxy, not capabilities echo.
                if type(settings) is not dict:
                    raise RuntimeError("invalid host live settings response")
            finally:
                self._config.timeout = original_timeout
            self._guard(deadline, records=True)
            self._active_observations(deadline)
            self._identity(
                deadline
            )  # Pin the original PID/start ticks, never reselect after the proxy.
            self._publish(
                "bound-session.json",
                {
                    "run_id": self._run_id,
                    "host_session_id": self._host_session_id,
                    "device_session_id": self._device_session_id,
                    "appium_server_url": self._server_url,
                    "server_identity": asdict(self._server_identity),
                    "correlation": "singleton downstream REST + live host proxy + bracketed OS identity",
                    "explicit_host_device_uuid_mapping": False,
                },
                deadline=deadline,
            )
            self._guard(deadline, records=True)
            self._bound_server_selector = (
                self._server_identity,
                self._host_session_id,
                self._device_session_id,
                self._system_port,
            )
            self._phase = "bound"
        except BaseException as error:
            self._failure = error
            raise

    def probe_server_status(self, *, deadline: float) -> None:
        """Observe the bound forwarding endpoint once; never infer session closure.

        The fresh socket bypasses Appium and its pool, not the phone lease. Keep
        response/error payloads only in this local diagnostic receipt. An HTTP
        failure consumes the attempt without invalidating independent cleanup.
        """
        self._scenario(deadline)
        if (
            self._finalized
            or self._phase != "bound"
            or self._server_identity is None
            or self._bound_server_selector is None
        ):
            raise RuntimeError(
                "server status requires the unfinalized bound Android session"
            )
        if self._server_status_attempted:
            raise RuntimeError("Android server status probe is one-shot")
        selector = self._bound_server_selector

        def guard() -> None:
            self._guard(deadline, records=True)
            if (
                self._finalized
                or self._phase != "bound"
                or type(self._system_port) is not int
                or selector
                != (
                    self._server_identity,
                    self._host_session_id,
                    self._device_session_id,
                    self._system_port,
                )
            ):
                raise RuntimeError("Android server status bound selector changed")

        guard()
        identity, host_session, device_session, port = selector
        base_url = f"http://127.0.0.1:{port}"
        started = time.monotonic()
        receipt = {
            "run_id": self._run_id,
            "host_session_id": host_session,
            "device_session_id": device_session,
            "server_identity": asdict(identity),
            "system_port": port,
            "endpoint": base_url + "/status",
            "started_monotonic": started,
            "finished_monotonic": None,
            "response": None,
            "error": None,
        }
        # Consume before socket dispatch, including HTTP and publication failures.
        self._server_status_attempted = True
        primary: BaseException | None = None
        boundary: BaseException | None = None
        try:
            receipt["response"] = read_loopback_json(
                base_url, "/status", deadline=min(deadline, started + 2.0)
            )
        except BaseException as error:  # noqa: BLE001 -- persist the observation before independent cleanup.
            primary = error
        try:
            guard()
        except BaseException as error:  # noqa: BLE001 -- stale authority must not be hidden by an HTTP failure.
            boundary = error
            if primary is not None and not isinstance(primary, Exception):
                primary.add_note(
                    f"Android server status post-read boundary: {type(boundary).__name__}: {boundary}"
                )
            else:
                if primary is not None:
                    boundary.add_note(
                        f"Android server status HTTP error: {type(primary).__name__}: {primary}"
                    )
                primary = boundary
        receipt["finished_monotonic"] = time.monotonic()
        if primary is not None:
            receipt["error"] = {
                "type": type(primary).__name__,
                "message": str(primary),
                "notes": list(getattr(primary, "__notes__", [])),
            }
        try:
            self._publish("server-status.json", receipt)
        except BaseException as publication:
            # Interrupts must reach the runner even if diagnostic cleanup fails.
            if primary is not None and not isinstance(primary, Exception):
                primary.add_note(
                    f"Android server status receipt: {type(publication).__name__}: {publication}"
                )
                raise primary
            if not isinstance(publication, Exception):
                if primary is not None:
                    publication.add_note(
                        f"Android server status prior error: {type(primary).__name__}: {primary}"
                    )
                raise
            if boundary is not None:
                boundary.add_note(
                    f"Android server status receipt: {type(publication).__name__}: {publication}"
                )
                raise boundary
            if primary is not None:
                publication.add_note(
                    f"Android server status HTTP error: {type(primary).__name__}: {primary}"
                )
            raise
        if primary is not None:
            raise primary

    def probe_adb_transport(self, *, deadline: float) -> None:
        """Observe only the existing 127.0.0.1:5037 daemon, once, in <=3 seconds.

        Do not use SDK/CLI adb here: adb_client.cpp's adb_check_server_version
        can auto-launch an absent daemon or kill a version-mismatched daemon.
        Pinning the SDK executable does not prevent those shared-host mutations.
        These four fixed read-only requests neither consult server endpoint env
        nor start/kill a daemon, change settings/radio, or create/remove forwards.
        boot_id is only an expected-value comparison, not ownership/close/RF proof.
        I/O failures are raised after recording, without invalidating cleanup.
        """
        self._scenario(deadline)
        if (
            self._finalized
            or self._phase != "bound"
            or self._server_identity is None
            or self._bound_server_selector is None
        ):
            raise RuntimeError(
                "ADB transport requires the unfinalized bound Android session"
            )
        if self._adb_transport_attempted:
            raise RuntimeError("Android ADB transport probe is one-shot")
        selector = self._bound_server_selector

        def guard() -> None:
            self._guard(deadline, records=True)
            if (
                self._finalized
                or self._phase != "bound"
                or type(self._system_port) is not int
                or self._bound_server_selector is not selector
                or selector
                != (
                    self._server_identity,
                    self._host_session_id,
                    self._device_session_id,
                    self._system_port,
                )
            ):
                raise RuntimeError("Android ADB transport bound selector changed")

        guard()
        identity, host_session, device_session, port = selector
        serial = self._device
        started = time.monotonic()
        probe_deadline = min(deadline, started + 3.0)
        receipt = {
            "run_id": self._run_id,
            "device": serial,
            "host_session_id": host_session,
            "device_session_id": device_session,
            "server_identity": asdict(identity),
            "system_port": port,
            "endpoint": "127.0.0.1:5037",
            "started_monotonic": started,
            "finished_monotonic": None,
            "budget_seconds": probe_deadline - started,
            "expected_boot_id": identity.boot_id,
            "boot_id_matches_expected": None,
            "stages": [],
            "error": None,
            "limitations": [
                "Existing daemon only; no CLI/SDK version negotiation, autostart or kill.",
                "Version and cached state do not attest daemon provenance or liveness.",
                "boot_id is an expected comparison, not ownership, session closure or RF proof.",
                "Bracketed observations are not atomic transport/process attestation.",
            ],
        }
        current: dict = {}

        @contextmanager
        def stage(name: str):
            nonlocal current
            current = {
                "name": name,
                "started_monotonic": time.monotonic(),
                "finished_monotonic": None,
                "output": "",
                "value": None,
                "error": None,
            }
            receipt["stages"].append(current)
            try:
                guard()
                self._remaining(probe_deadline)
                yield
                self._remaining(probe_deadline)
            except BaseException as error:
                current["error"] = {"type": type(error).__name__, "message": str(error)}
                raise
            finally:
                current["finished_monotonic"] = time.monotonic()

        def connect():
            return socket.create_connection(
                ("127.0.0.1", 5037), timeout=self._remaining(probe_deadline)
            )

        def read(connection, maximum: int) -> bytes:
            connection.settimeout(self._remaining(probe_deadline))
            data = connection.recv(maximum)
            current["output"] += data.decode("ascii", errors="backslashreplace")
            self._remaining(probe_deadline)
            return data

        def exact(connection, count: int) -> bytes:
            data = bytearray()
            while len(data) < count:
                chunk = read(connection, count - len(data))
                if not chunk:
                    raise RuntimeError("ADB frame unexpected EOF")
                data.extend(chunk)
            return bytes(data)

        def payload(connection) -> bytes:
            length = exact(connection, 4)
            if re.fullmatch(rb"[0-9a-fA-F]{4}", length) is None:
                raise RuntimeError("ADB invalid payload length")
            size = int(length, 16)
            if size > 64:
                raise RuntimeError("ADB payload exceeds 64-byte limit")
            return exact(connection, size)

        def okay(connection) -> None:
            status = exact(connection, 4)
            if status == b"FAIL":
                message = payload(connection).decode("ascii", errors="backslashreplace")
                raise RuntimeError(f"ADB FAIL: {message}")
            if status != b"OKAY":
                raise RuntimeError(f"ADB invalid status: {status!r}")

        def send(connection, command: bytes) -> None:
            connection.settimeout(self._remaining(probe_deadline))
            connection.sendall(f"{len(command):04x}".encode("ascii") + command)
            self._remaining(probe_deadline)

        # Consume before any socket I/O, including connection/publication failure.
        self._adb_transport_attempted = True
        primary: BaseException | None = None
        boundary: BaseException | None = None
        try:
            with stage("server_version"), connect() as connection:
                send(connection, b"host:version")
                okay(connection)
                version = payload(connection)
                current["value"] = version.decode("ascii", errors="backslashreplace")
                if re.fullmatch(rb"[0-9a-fA-F]{4}", version) is None:
                    raise RuntimeError("ADB invalid server version")
            with stage("cached_state"), connect() as connection:
                send(connection, f"host-serial:{serial}:get-state".encode("ascii"))
                okay(connection)
                state = payload(connection)
                current["value"] = state.decode("ascii", errors="backslashreplace")
                if state != b"device":
                    raise RuntimeError("ADB cached state is not device")
            # The third fresh socket selects the exact serial, then one service.
            with ExitStack() as sockets:
                with stage("transport_select"):
                    connection = sockets.enter_context(connect())
                    send(connection, f"host:transport:{serial}".encode("ascii"))
                    okay(connection)
                    current["value"] = "OKAY"
                with stage("service_accept"):
                    send(connection, b"shell:cat /proc/sys/kernel/random/boot_id")
                    okay(connection)
                    current["value"] = "OKAY"
                with stage("boot_id"):
                    boot = bytearray()
                    while True:
                        chunk = read(connection, 129 - len(boot))
                        if not chunk:
                            break
                        boot.extend(chunk)
                        if len(boot) > 128:
                            raise RuntimeError("ADB boot_id exceeds 128-byte limit")
                    if (
                        re.fullmatch(
                            rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n",
                            boot,
                        )
                        is None
                    ):
                        raise RuntimeError("ADB invalid boot_id UUID/newline")
                    current["value"] = boot[:-1].decode("ascii")
                    receipt["boot_id_matches_expected"] = (
                        current["value"] == identity.boot_id
                    )
                    if not receipt["boot_id_matches_expected"]:
                        raise RuntimeError("ADB boot_id differs from expected boot_id")
        except BaseException as error:  # noqa: BLE001 -- keep the observation before independent cleanup.
            primary = error
        try:
            guard()
        except BaseException as error:  # noqa: BLE001 -- stale authority must not be hidden by I/O failure.
            boundary = error
            if primary is not None and not isinstance(primary, Exception):
                primary.add_note(
                    f"Android ADB transport post-read boundary: {type(boundary).__name__}: {boundary}"
                )
            else:
                if primary is not None:
                    boundary.add_note(
                        f"Android ADB transport I/O error: {type(primary).__name__}: {primary}"
                    )
                primary = boundary
        receipt["finished_monotonic"] = time.monotonic()
        if primary is not None:
            receipt["error"] = {
                "type": type(primary).__name__,
                "message": str(primary),
                "notes": list(getattr(primary, "__notes__", [])),
            }
        try:
            self._publish("adb-transport.json", receipt)
        except BaseException as publication:
            if primary is not None and not isinstance(primary, Exception):
                primary.add_note(
                    f"Android ADB transport receipt: {type(publication).__name__}: {publication}"
                )
                raise primary
            if not isinstance(publication, Exception):
                if primary is not None:
                    publication.add_note(
                        f"Android ADB transport prior error: {type(primary).__name__}: {primary}"
                    )
                raise
            if boundary is not None:
                boundary.add_note(
                    f"Android ADB transport receipt: {type(publication).__name__}: {publication}"
                )
                raise boundary
            if primary is not None:
                publication.add_note(
                    f"Android ADB transport I/O error: {type(primary).__name__}: {primary}"
                )
            raise
        if primary is not None:
            raise primary

    def capture_server_logs(self, *, deadline: float) -> None:
        """Read the original bound server's logs once, without refreshing authority."""
        self._scenario(deadline)
        if self._finalized or self._phase != "bound" or self._server_identity is None:
            raise RuntimeError(
                "server logs require the unfinalized bound Android session"
            )
        if self._server_logs_attempted:
            raise RuntimeError("Android server log capture is one-shot")
        self._guard(deadline, records=True)
        # Consume before dispatch: a failed read/receipt must never replay logcat.
        self._server_logs_attempted = True
        self._command(
            deadline,
            "logcat",
            "-d",
            "-v",
            "threadtime",
            "--pid",
            str(self._server_identity.pid),
            "-t",
            "1000",
            "appium:I",
            "*:S",
        )

    def session_deleted(self, host_session_id: str, *, deadline: float) -> None:
        if (
            self._finalized
            or self._phase != "bound"
            or host_session_id != self._host_session_id
        ):
            raise RuntimeError("DELETE receipt must name the bound host session")
        self._phone_guard(deadline)
        self._driver = self._executor = self._config = None
        self._phase = "deleted"

    def finalize(self, deadline: float) -> None:
        if self._finalized:
            if self._close_failure is not None:
                raise self._close_failure
            raise RuntimeError("Android automation finalization is one-shot")
        self._remaining(deadline)
        self._finalized = True
        try:
            if self._phase not in {"prepared", "deleted"}:
                raise RuntimeError(
                    "unknown or undeleted Android session; refusing cleanup success"
                )
            self._guard(deadline, records=True)
            data, extra = self._idle("final", deadline)
            self._same_baseline(data, extra)
            if self._settings_identity is not None:
                android_sdk.read_app_identity(
                    serial=self._device,
                    package=SETTINGS_PACKAGE,
                    expected_apk_sha256=self._expected[SETTINGS_PACKAGE],
                    expected_identity=self._settings_identity,
                    cwd=self._cwd,
                    deadline=deadline,
                    require_owner=lambda: self._guard(deadline, records=True),
                )
            self._publish(
                "finalized.json",
                {
                    "run_id": self._run_id,
                    "status": "passed",
                    "scope": "observed Android automation cleanup",
                    "host_gate_status": "pending",
                    "session_deleted": self._phase == "deleted",
                    "resource_zero": "not-claimed",
                },
                deadline=deadline,
            )
            self._guard(deadline, records=True)
            self._phase = "closed"
        except BaseException as error:
            self._close_failure = error
            try:
                self._publish(
                    "finalize-failed.json",
                    {
                        "run_id": self._run_id,
                        "status": "failed",
                        "error": "".join(
                            format_exception_only(error, show_group=True)
                        ).strip(),
                    },
                )
            except BaseException as publication:  # noqa: BLE001 -- finalization's primary cause is never replaced.
                error.add_note(f"Android finalization evidence: {publication}")
            raise

    def require_helper_scope(
        self,
        *,
        phone: AndroidRadioController,
        run_id: str,
        appium_home: Path,
        expected_apk_sha256: dict[str, str],
        system_port: int,
        scenario_deadline: float,
        deadline: float,
    ) -> None:
        """Check a concrete helper handoff, not session admission or closure.

        The frozen scenario deadline identifies the scope. The observation itself
        may use the independent cleanup deadline without extending that scenario.
        """
        self._phone_guard(deadline)
        if (
            phone is not self._phone
            or run_id != self._run_id
            or appium_home.absolute() != self._appium_home
            or expected_apk_sha256 != self._expected
            or system_port != self._system_port
            or scenario_deadline != self._deadline
        ):
            raise RuntimeError("helper handoff belongs to another automation scope")
        self._records_current()
        self._phone_guard(deadline)

    def require_helper_restoration(
        self,
        *,
        phone: AndroidRadioController,
        run_id: str,
        appium_home: Path,
        expected_apk_sha256: dict[str, str],
        system_port: int,
        deadline: float,
    ) -> None:
        """Permit helper rollback after known cleanup or before session submission.

        Preparation/admission failures can occur before any Remote request. They
        do not need a fabricated DELETE receipt; requesting or binding failures
        still cannot be washed clean by an apparently idle device inventory.
        """
        self.require_helper_scope(
            phone=phone,
            run_id=run_id,
            appium_home=appium_home,
            expected_apk_sha256=expected_apk_sha256,
            system_port=system_port,
            scenario_deadline=self._deadline,
            deadline=deadline,
        )
        if self._phase == "closed":
            self.require_closed()
        elif (
            self._phase not in {"new", "preparing", "prepared", "admitting"}
            or self._finalized
            or self._server_url is not None
            or self._driver is not None
            or self._host_session_id is not None
            or self._device_session_id is not None
        ):
            raise RuntimeError(
                "unknown or unclosed Android session blocks helper restoration"
            )
        self._records_current()
        self._phone_guard(deadline)

    def require_closed(self) -> None:
        if self._phase != "closed" or self._close_failure is not None:
            raise RuntimeError(
                "Android automation cleanup is not verified"
            ) from self._close_failure
        try:
            self._records_current()
        except Exception as error:
            self._close_failure = error
            raise
