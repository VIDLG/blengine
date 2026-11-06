"""Host-only Linking relay for the multifunction cancellation barrier."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840.cancellation_control import (
    Control,
    ControlRelay,
    SuiteControlRelay,
    read_document,
    write_document,
)
from tools.react_native import android_sdk
from tools.react_native.host_contract import APP_NAME, BUNDLE_ID, SCHEME
from tools.react_native.ios_sdk import (
    devicectl_post_notification,
    official_result,
)

IOS_BOOTSTRAP_SECONDS = 45.0


def android_pid(serial: str) -> str:
    result = android_sdk.run_command(
        android_sdk.adb_command(serial, "shell", "pidof", "com.blehubnitrohost"),
        timeout=1,
        check=True,
    ).stdout.strip()
    if not result.isdecimal() or int(result) <= 0:
        raise RuntimeError("expected one live Android RN PID")
    return result


def relay_android(serial: str, pid: str, control: Control, phase: str) -> None:
    if android_pid(serial) != pid:
        raise RuntimeError("Android RN PID changed before RF signal")
    result = android_sdk.run_command(
        android_sdk.adb_command(
            serial,
            "shell",
            "am",
            "start",
            "-n",
            "com.blehubnitrohost/.MainActivity",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            control.uri(phase),
        ),
        timeout=1.5,
        check=True,
    )
    if "Error:" in result.stdout or "Exception" in result.stdout:
        raise RuntimeError("Android RF signal intent failed")
    if android_pid(serial) != pid:
        raise RuntimeError("Android RF signal restarted the runtime")


def _command_output(value: bytes | str | None) -> str:
    return (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else value or ""
    )


def _run_ios_command(command: list[str], *, timeout: float) -> None:
    """Keep the host budget independent of devicectl's five-second CLI floor."""
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        error.add_note(
            f"devicectl outer timeout={timeout:g}s; stderr:\n{_command_output(error.stderr)}"
        )
        raise


class IosRuntimeIdentity:
    """Pin the scoped native bootstrap against install/process receipts, not exit JSON."""

    def __init__(
        self,
        device: str,
        control: Control,
        install_json: Path,
        log: Path,
        controls: tuple[Control, ...] | None = None,
    ) -> None:
        self.device, self.control, self.log = device, control, log
        self.controls = controls or (control,)
        self.schedule = [(c, phase) for c in self.controls for phase in c.phases]
        self.phase_keys = [self._phase_key(c, p) for c, p in self.schedule]
        installed = official_result(
            install_json, "devicectl.device.install.app", device
        )
        rows = installed.get("installedApplications")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("invalid iOS install application identity")
        matches = [row for row in rows if row.get("bundleID") == BUNDLE_ID]
        if len(matches) != 1 or not isinstance(matches[0].get("installationURL"), str):
            raise RuntimeError(
                "iOS install receipt does not uniquely identify the host"
            )
        url = matches[0]["installationURL"]
        if not url.startswith("file:///") or not url.endswith("/" + APP_NAME + "/"):
            raise RuntimeError(
                "iOS install receipt lacks the full host installation URL"
            )
        self.device_identifier = installed["deviceIdentifier"]
        self.executable = url + SCHEME
        self.install_json = install_json
        self.bootstrap: dict[str, Any] | None = None
        self.sent: list[str] = []
        self.acknowledged: list[str] = []
        self.js_acknowledged: list[str] = []
        self.sent_at = 0.0
        self.launch_started_at: float | None = None
        self.bootstrap_deadline: float | None = None
        self.complete = False
        self.failure: str | None = None
        self.failure_command: dict[str, Any] | None = None

    def _phase_key(self, control: Control, phase: str) -> str:
        return phase if len(self.controls) == 1 else f"{control.flow}:{phase}"

    def begin_launch(self, started_at: float, suite_deadline: float) -> None:
        if self.launch_started_at is not None:
            raise RuntimeError("iOS launch boot window cannot be restarted")
        self.launch_started_at = started_at
        self.bootstrap_deadline = min(
            started_at + IOS_BOOTSTRAP_SECONDS, suite_deadline
        )
        self._record()

    def fail(self, error: BaseException) -> None:
        self.failure = str(error)
        if isinstance(
            error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)
        ):
            self.failure_command = {
                "command": error.cmd,
                "returncode": getattr(error, "returncode", None),
                "timeout": error.timeout
                if isinstance(error, subprocess.TimeoutExpired)
                else None,
                "stdout": _command_output(error.stdout),
                "stderr": _command_output(error.stderr),
                "notes": getattr(error, "__notes__", []),
            }
        self._record()

    def _record(self) -> None:
        write_document(
            self.log.with_suffix(".rf-host-identity.json"),
            {
                "schema_version": 1,
                "controls": [asdict(c) for c in self.controls],
                "device": self.device,
                "device_identifier": self.device_identifier,
                "install_json": str(self.install_json),
                "bootstrap": self.bootstrap,
                "launch_started_at": self.launch_started_at,
                "bootstrap_deadline": self.bootstrap_deadline,
                "sent": self.sent,
                "native_acknowledged": self.acknowledged,
                "js_acknowledged": self.js_acknowledged,
                "status": "failed"
                if self.failure is not None
                else "passed"
                if self.complete
                else "pinned"
                if self.bootstrap is not None
                else "awaiting-bootstrap",
                "failure": self.failure,
                "failure_command": self.failure_command,
            },
        )

    def _verify_process(
        self, record: dict[str, Any], boundary: str, timeout: int
    ) -> None:
        path = self.log.with_suffix(f".rf-host-{boundary}-processes.json")
        path.unlink(missing_ok=True)
        _run_ios_command(
            [
                "xcrun",
                "devicectl",
                "device",
                "info",
                "processes",
                "--device",
                self.device,
                "--quiet",
                "--timeout",
                str(max(5, timeout)),
                "--json-output",
                str(path),
            ],
            timeout=timeout + 0.5,
        )
        result = official_result(path, "devicectl.device.info.processes", self.device)
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
            raise RuntimeError("foreign or malformed iOS process identity")
        matches = [row for row in rows if row["processIdentifier"] == record["pid"]]
        if len(matches) != 1 or matches[0].get("executable") != record["executable"]:
            raise RuntimeError(
                "pinned iOS PID/executable is absent, duplicated or changed"
            )

    def _require_live(self) -> None:
        if self.failure is not None:
            raise RuntimeError(f"iOS runtime identity failed: {self.failure}")

    def _check_ack_deadline(self) -> None:
        self._require_live()
        if time.monotonic() - self.sent_at >= 2:
            error = TimeoutError("native/JS iOS signal ACK deadline exceeded")
            self.fail(error)
            raise error

    def observe(self, line: str) -> None:
        self._require_live()
        prefix = "BLEHUB_NITRO_RUNTIME rf-host:"
        if "BLEHUB_NITRO_RUNTIME rf-control:" in line:
            if self.bootstrap is None:
                raise RuntimeError("iOS bootstrap identity missing before control/ARM")
            for control, phase in self.schedule:
                key = self._phase_key(control, phase)
                if control.marker("observed", phase) in line:
                    if key not in self.sent:
                        raise RuntimeError("JS iOS signal ACK preceded forwarding")
                    if (
                        len(self.js_acknowledged) >= len(self.phase_keys)
                        or key != self.phase_keys[len(self.js_acknowledged)]
                    ):
                        raise RuntimeError(
                            "duplicate or out-of-order JS iOS signal ACK"
                        )
                    self._check_ack_deadline()
                    self.js_acknowledged.append(key)
                    self._record()
        if prefix not in line:
            return
        kind, payload = line.split(prefix, 1)[1].strip().split(":", 1)
        record = json.loads(payload)
        if not isinstance(record, dict):
            raise TypeError("invalid scoped iOS host record")
        if kind == "bootstrap":
            self.ready_for_relay("")
            expected = {
                "session": self.control.session,
                "flow": self.control.flow,
                "mode": self.control.mode,
                "token": self.control.token,
                "bundle_id": BUNDLE_ID,
                "executable": self.executable,
            }
            if (
                self.bootstrap is not None
                or set(record) != set(expected) | {"pid", "boot_id"}
                or any(
                    type(record[key]) is not type(value) or record[key] != value
                    for key, value in expected.items()
                )
                or type(record["pid"]) is not int
                or record["pid"] <= 0
                or not isinstance(record["boot_id"], str)
                or str(uuid.UUID(record["boot_id"])) != record["boot_id"]
            ):
                raise RuntimeError(
                    "foreign, malformed or duplicate iOS bootstrap identity"
                )
            self._verify_process(record, "bootstrap", 5)
            self.bootstrap = record
        elif kind == "signal":
            index = len(self.acknowledged)
            if (
                self.bootstrap is None
                or index >= len(self.sent)
                or record
                != {
                    **self.bootstrap,
                    **asdict(self.schedule[index][0]),
                    "phase": self.schedule[index][1],
                }
            ):
                raise RuntimeError("foreign, early or duplicate native iOS signal ACK")
            self._check_ack_deadline()
            self.acknowledged.append(self.sent[index])
        else:
            raise RuntimeError("unknown scoped iOS host record")
        self._record()

    def ready_for_relay(self, text: str) -> bool:
        self._require_live()
        if self.bootstrap is None:
            if "BLEHUB_NITRO_RUNTIME rf-control:" in text:
                raise RuntimeError("iOS bootstrap identity missing before control/ARM")
            if self.bootstrap_deadline is None:
                raise RuntimeError(
                    "iOS bootstrap startup window is not launch-anchored"
                )
            if time.monotonic() >= self.bootstrap_deadline:
                raise TimeoutError(
                    "iOS bootstrap startup deadline exceeded without native boot"
                )
            return False
        # Linking delivery and native stdout can race across RN/native threads.
        # Neither ACK alone may publish the next provider request.
        if len(self.acknowledged) < len(self.sent) or len(self.js_acknowledged) < len(
            self.sent
        ):
            self._check_ack_deadline()
            return False
        return True

    def before_signal(self, phase: str, control: Control | None = None) -> None:
        self._require_live()
        key = self._phase_key(control or self.control, phase)
        if (
            self.bootstrap is None
            or self.sent != self.acknowledged
            or self.sent != self.js_acknowledged
            or len(self.sent) >= len(self.phase_keys)
            or key != self.phase_keys[len(self.sent)]
            or (control or self.control, phase) != self.schedule[len(self.sent)]
        ):
            raise RuntimeError("iOS signal identity/ACK phase drift")
        self._verify_process(self.bootstrap, key.replace(":", "-"), 1)

    def sent_signal(self, phase: str, control: Control | None = None) -> None:
        self._require_live()
        key = self._phase_key(control or self.control, phase)
        if (
            self.bootstrap is None
            or self.sent != self.acknowledged
            or self.sent != self.js_acknowledged
            or len(self.sent) >= len(self.phase_keys)
            or key != self.phase_keys[len(self.sent)]
            or (control or self.control, phase) != self.schedule[len(self.sent)]
        ):
            raise RuntimeError("iOS signal identity/ACK phase drift")
        self.sent.append(key)
        self.sent_at = time.monotonic()
        self._record()

    def finish(self, text: str) -> None:
        self._require_live()
        if self.complete:
            return
        if (
            self.bootstrap is None
            or self.sent != self.phase_keys
            or self.acknowledged != self.phase_keys
            or self.js_acknowledged != self.phase_keys
            or any(
                text.count(control.marker("observed", phase)) != 1
                for control, phase in self.schedule
            )
        ):
            raise RuntimeError(
                "iOS runtime identity requires all native and JS signal ACKs"
            )
        self._verify_process(self.bootstrap, "closed", 5)
        self.complete = True
        self._record()


def check_suite_identity(text: str, log: Path, root: Path) -> None:
    """Offline correlate the captured runtime with the live runner's fixed identity."""
    from tests.peripherals.nrf52840.cancellation_control import load_suite_controls

    controls = load_suite_controls(root)
    owner_path = log.with_suffix(".capture.json")
    if (
        owner_path.exists()
        and read_document(owner_path).get("scenario") == "full-suite"
    ):
        from tools.react_native.full_suite_ledger import check_base_identity

        check_base_identity(text, log, root)
        return
    ios_path = log.with_suffix(".rf-host-identity.json")
    if not ios_path.exists():
        # The iOS runner retains its launch capture and publishes an exact final copy.
        source = log.with_name(log.stem + ".attempt.log")
        source_identity = source.with_suffix(".rf-host-identity.json")
        if source.is_file() and source_identity.is_file():
            if source.read_text(encoding="utf-8") != text:
                raise ValueError("final iOS log differs from its pinned launch capture")
            log, ios_path = source, source_identity
    if ios_path.exists():
        identity = read_document(ios_path)
        bootstrap = identity.get("bootstrap")
        keys = [
            f"{control.flow}:{phase}"
            for control in controls
            for phase in control.phases
        ]
        if (
            identity.get("status") != "passed"
            or identity.get("failure") is not None
            or identity.get("controls") != [asdict(c) for c in controls]
            or any(
                identity.get(field) != keys
                for field in ("sent", "native_acknowledged", "js_acknowledged")
            )
            or not isinstance(bootstrap, dict)
        ):
            raise ValueError("iOS continuous native identity/ACK proof incomplete")
        if (
            any(
                bootstrap.get(key) != value
                for key, value in asdict(controls[0]).items()
            )
            or type(bootstrap.get("pid")) is not int
            or bootstrap["pid"] <= 0
            or bootstrap.get("bundle_id") != BUNDLE_ID
            or not isinstance(bootstrap.get("executable"), str)
            or not bootstrap["executable"].startswith("file:///")
            or not bootstrap["executable"].endswith("/" + APP_NAME + "/" + SCHEME)
            or str(uuid.UUID(bootstrap["boot_id"])) != bootstrap["boot_id"]
        ):
            raise ValueError("iOS continuous bootstrap drift")
        records = re.findall(
            r"BLEHUB_NITRO_RUNTIME rf-host:(bootstrap|signal):([^\r\n]+)", text
        )
        expected = [
            ("bootstrap", bootstrap),
            *[
                ("signal", {**bootstrap, **asdict(c), "phase": phase})
                for c in controls
                for phase in c.phases
            ],
        ]
        if [(kind, json.loads(payload)) for kind, payload in records] != expected:
            raise ValueError("iOS native signal boot/PID/executable changed")
        for boundary in ("bootstrap", "closed"):
            result = official_result(
                log.with_suffix(f".rf-host-{boundary}-processes.json"),
                "devicectl.device.info.processes",
                identity["device"],
            )
            matches = [
                row
                for row in result["runningProcesses"]
                if row.get("processIdentifier") == bootstrap["pid"]
            ]
            if (
                result["deviceIdentifier"] != identity["device_identifier"]
                or len(matches) != 1
                or matches[0].get("executable") != bootstrap["executable"]
            ):
                raise ValueError("iOS official runtime PID/device/executable drift")
    else:
        identity = read_document(log.with_suffix(".identity.json"))
        capture = read_document(log.with_suffix(".capture.json"))
        pid = identity.get("pid")
        command = capture.get("command", [])
        if (
            identity.get("status") != "pinned"
            or not isinstance(pid, str)
            or not pid.isdecimal()
            or int(pid) <= 0
            or capture.get("status") != "passed"
            or capture.get("controls") != [asdict(c) for c in controls]
            or capture.get("runtime_pid") != pid
            or command.count("--pid") != 1
            or command[command.index("--pid") + 1] != pid
        ):
            raise ValueError("Android fixed runtime/capture identity proof incomplete")
        for line in text.splitlines():
            if "BLEHUB_NITRO_RUNTIME " in line:
                header = re.match(r"\S+\s+\S+\s+(\d+)\s+", line)
                if header is None or header.group(1) != pid:
                    raise ValueError("foreign Android PID runtime marker")


def relay_ios(udid: str, control: Control, phase: str) -> None:
    _run_ios_command(
        devicectl_post_notification(udid, control.notification(phase)),
        timeout=1.5,
    )


def advance_relay(
    relay: ControlRelay | SuiteControlRelay,
    text: str,
    send: Callable[[Control, str], None],
) -> None:
    relay.observe(text)
    if isinstance(relay, SuiteControlRelay):
        pending = relay.pending_control()
        if pending is None:
            return
        owner, phase = pending
    else:
        owner, phase = relay, relay.pending()
    if phase is not None:
        send(owner.control, phase)
        owner.forwarded(phase)
