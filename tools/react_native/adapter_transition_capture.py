"""One Android launch, one streaming logcat and one same-process adapter URL.

The parent owns the phone lock, OFF readback, frozen installation and fixture.
This owner never retries a launch/control or claims RF from capture alone.
"""

from __future__ import annotations

import hashlib
import math
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote, unquote

from tests.peripherals.nrf52840.adapter_transition import (
    ARM_NAME,
    LOSS_NAME,
    MODE,
    OFF_CALL_SECONDS,
    OFF_CONFIRMED,
    OFF_READY,
    OFF_REQUEST,
    PHYSICAL_NAME,
    PREFIX,
    RuntimeFacts,
    adapter_mode,
    signal_url,
)
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fault_control import (
    Operation,
    Phase,
    owner_nonce,
    status_from_document,
    ticket_from_document,
)
from tests.peripherals.nrf52840.fixture_session import _json, _read_bytes
from tools.react_native import android_sdk
from tools.react_native.adapter_transition_ledger import verify_readiness
from tools.react_native.android_rf_host import wait_for_initial_pid
from tools.react_native.capture_io import MAX_BYTES, PrefixStream, stop_local
from tools.react_native.capture_transport import (
    BOOT_SECONDS,
    COMMAND_SECONDS,
    POLL_SECONDS,
    PROCESS_SECONDS,
)
from tools.react_native.host_contract import ANDROID_ACTIVITY, ANDROID_PACKAGE

RAW_PREFIX = "BLEHUB_NITRO_RUNTIME "
COMPONENT = f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}"
ACK_SECONDS = 2.0
DELIVERED_WARNING = (
    "Warning: Activity not started, intent has been delivered to currently running "
    "top-most instance."
)
READY_NAME = "adapter-transition-ready.json"


def _stop_owned_app(
    device: str, pid: int, executable: str, phone_guard: Callable[[], None]
) -> None:
    def query(*args: str) -> str:
        phone_guard()
        return android_sdk.run_command(
            android_sdk.adb_command(device, *args), timeout=2, check=True
        ).stdout.strip()

    if query("shell", "pidof", ANDROID_PACKAGE) != str(pid):
        raise RuntimeError("refusing to stop a replaced/absent Android PID")
    if query("shell", "pm", "path", ANDROID_PACKAGE) != (
        "package:" + unquote(executable.removeprefix("file:"))
    ):
        raise RuntimeError("refusing cleanup of a changed installed sourceDir")
    # Recheck after the sourceDir query, immediately before the destructive command.
    if query("shell", "pidof", ANDROID_PACKAGE) != str(pid):
        raise RuntimeError("refusing to stop a replaced/absent Android PID")
    query("shell", "am", "force-stop", ANDROID_PACKAGE)


def abort_owned_app(
    device: str, log: Path, run_id: str, *, phone_guard: Callable[[], None]
) -> None:
    """Parent-only cleanup after a passed capture fails a later pure checker."""
    identity = read_document(log.with_suffix(".identity.json"))
    bootstrap = identity.get("native_bootstrap")
    if (
        identity.get("schema_version") != 1
        or identity.get("platform") != "android"
        or identity.get("scenario") != "adapter-transition"
        or identity.get("status") != "passed"
        or identity.get("run_id") != run_id
        or not isinstance(bootstrap, dict)
        or type(identity.get("pid")) is not int
        or identity["pid"] <= 0
        or bootstrap.get("pid") != identity["pid"]
        or bootstrap.get("run_id") != run_id
        or bootstrap.get("boot_id") != identity.get("boot_id")
        or bootstrap.get("bundle_id") != ANDROID_PACKAGE
        or not isinstance(bootstrap.get("executable"), str)
        or not bootstrap["executable"].startswith("file:/")
    ):
        raise RuntimeError(
            "refusing cleanup without a passed same-run capture identity"
        )
    _stop_owned_app(device, identity["pid"], bootstrap["executable"], phone_guard)


def validate_inputs(device: str, timeout: float) -> None:
    if (
        not isinstance(device, str)
        or not device
        or device.startswith("-")
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in device)
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValueError(
            "explicit Android device and finite timeout in (0, 300] required"
        )


class AdapterCapture:
    """Capture owner; all children are local, only the pinned app is cleanup-owned."""

    def __init__(
        self,
        device: str,
        log: Path,
        coordination: Path,
        run_id: str,
        service_uuid: str,
        local_name: str,
        *,
        timeout: float,
        guard: Callable[[], None],
        radio_on: Callable[[], None],
        phone_guard: Callable[[], None] = lambda: None,
        mode: str = "startup-off",
        radio_off: Callable[..., None] | None = None,
    ) -> None:
        validate_inputs(device, timeout)
        for value in (run_id, service_uuid):
            if str(uuid.UUID(value)) != value:
                raise ValueError("canonical run/service UUIDs required")
        if not local_name.strip() or len(local_name) > 512:
            raise ValueError("bounded fixture local name required")
        self.mode = adapter_mode(mode)
        if mode == "active-loss" and radio_off is None:
            raise ValueError("active loss requires an owned radio OFF action")
        self.radio_off = radio_off
        self.arm_sent_at: float | None = None
        self.arm_native_ack = self.arm_js_ack = False
        self.off_requested_at: float | None = None
        self.off_started_at: float | None = None
        self.off_finished_at: float | None = None
        self.active_documents: dict[str, bytes] = {}
        self.device, self.log, self.root = device, log, coordination
        self.run_id, self.service, self.name = run_id, service_uuid, local_name
        self.guard, self.radio_on, self.phone_guard = guard, radio_on, phone_guard
        self.receipt_path = self.root / "fixture-session/fixture-receipt.json"
        self.receipt_sha256 = hashlib.sha256(_read_bytes(self.receipt_path)).hexdigest()
        self.ready_raw: bytes | None = None
        self.deadline = time.monotonic() + timeout
        self.boot_deadline: float | None = None
        self.sent_at: float | None = None
        self.native_ack = self.js_ack = False
        self.on_confirmed = False
        self.pid: int | None = None
        self.executable = ""
        self.lines: list[str] = []
        self.facts = RuntimeFacts(
            run_id, service_uuid, local_name, require_readiness=True, mode=mode
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.output: BinaryIO | None = None
        self.stream: PrefixStream | None = None
        self.next_identity = time.monotonic()
        self.receipts: list[dict[str, Any]] = []

    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def _publish(self) -> None:
        # Replace the complete cumulative snapshot, never expose a truncated prefix
        # to the independently running provider's RuntimeFacts.refresh().
        for path in (self.log, self.root / "runtime.log"):
            temporary = path.with_name(path.name + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    stream.write(self.text())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def _identity(self, status: str) -> None:
        bootstrap = self.facts.bootstrap
        write_document(
            self.log.with_suffix(".identity.json"),
            {
                "schema_version": 1,
                **(
                    {"adapter_mode": self.mode}
                    if bootstrap and "adapter_mode" in bootstrap
                    else {}
                ),
                "platform": "android",
                "scenario": "adapter-transition",
                "status": status,
                "run_id": self.run_id,
                "pid": self.pid,
                "boot_id": bootstrap["boot_id"] if bootstrap else None,
                "native_bootstrap": bootstrap,
                "process_receipts": self.receipts,
                "signal_sent_monotonic": self.sent_at,
                **(
                    {
                        "active_actions": {
                            "arm_sent_monotonic": self.arm_sent_at,
                            "off_requested_monotonic": self.off_requested_at,
                            "off_started_monotonic": self.off_started_at,
                            "off_finished_monotonic": self.off_finished_at,
                            "arm_native_ack": self.arm_native_ack,
                            "arm_js_ack": self.arm_js_ack,
                        }
                    }
                    if self.mode == "active-loss"
                    else {}
                ),
            },
        )

    def _ack_deadline(self) -> None:
        if (
            self.arm_sent_at is not None
            and not self.arm_js_ack
            and time.monotonic() >= self.arm_sent_at + ACK_SECONDS
        ):
            raise TimeoutError("active ARM native/JS ACK deadline exceeded")
        if (
            self.sent_at is not None
            and not self.js_ack
            and time.monotonic() >= self.sent_at + ACK_SECONDS
        ):
            raise TimeoutError("native/JS adapter ACK exceeded original 2s deadline")

    def _observe(self, line: str) -> None:
        if not line.startswith(PREFIX):
            raise RuntimeError("foreign BLEHUB runtime prefix")
        marker = line[len(PREFIX) :]
        if marker.startswith("native:bootstrap:"):
            value = _json(marker.removeprefix("native:bootstrap:").encode())
            if (
                type(value.get("pid")) is not int
                or value["pid"] != self.pid
                or value.get("executable") != self.executable
                or str(uuid.UUID(value["boot_id"])) != value["boot_id"]
            ):
                raise RuntimeError("foreign bootstrap PID/executable/boot identity")
            if self.boot_deadline is None or time.monotonic() >= self.boot_deadline:
                raise TimeoutError(
                    "launch-anchored adapter bootstrap deadline exceeded"
                )
        elif marker.startswith("native:arm:"):
            self._ack_deadline()
            if self.arm_sent_at is None or self.arm_native_ack:
                raise RuntimeError("early/duplicate native ARM ACK")
        elif marker.startswith("active:arm-signal:"):
            self._ack_deadline()
            if self.arm_sent_at is None or not self.arm_native_ack or self.arm_js_ack:
                raise RuntimeError("early/duplicate JS ARM ACK")
        elif marker.startswith("native:signal:"):
            self._ack_deadline()
            if self.sent_at is None or not self.on_confirmed or self.native_ack:
                raise RuntimeError("early/duplicate native adapter ACK")
        elif marker.startswith("on-signal:"):
            self._ack_deadline()
            if self.sent_at is None or not self.native_ack or self.js_ack:
                raise RuntimeError("early/duplicate JS adapter ACK")
        self.facts.observe(line)
        if marker.startswith("native:arm:"):
            self.arm_native_ack = True
        elif marker.startswith("active:arm-signal:"):
            self.arm_js_ack = True
        if marker.startswith("native:signal:"):
            self.native_ack = True
        elif marker.startswith("on-signal:"):
            self.js_ack = True

    def _ready(self) -> bool:
        path = self.root / READY_NAME
        if not path.exists() and not path.is_symlink():
            if self.ready_raw is not None:
                raise RuntimeError("adapter ready ACK disappeared")
            return False
        raw = _read_bytes(path)
        if self.ready_raw is not None:
            if raw != self.ready_raw:
                raise RuntimeError("adapter ready ACK changed")
            return True
        if self.facts.stage != 7 or self.facts.bootstrap is None:
            raise RuntimeError("early adapter ready ACK before closed OFF/awaiting-on")
        value = _json(raw)
        try:
            verify_readiness(
                value,
                receipt_sha256=self.receipt_sha256,
                runtime={
                    "run_id": self.run_id,
                    "native_bootstrap": self.facts.bootstrap,
                    **(
                        {"adapter_mode": self.mode}
                        if self.facts.bootstrap
                        and "adapter_mode" in self.facts.bootstrap
                        else {}
                    ),
                },
            )
        except ValueError as error:
            raise RuntimeError("foreign/malformed adapter ready ACK") from error
        if (
            hashlib.sha256(_read_bytes(self.receipt_path)).hexdigest()
            != self.receipt_sha256
        ):
            raise RuntimeError("fixture receipt bytes changed before adapter ready ACK")
        self.ready_raw = raw
        self.facts.on_authorized = True
        return True

    def _poll(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("adapter capture absolute deadline exceeded")
        self._ack_deadline()
        if self.stream is not None:
            lines = self.stream.poll()
            # Keep rejected records too; no deduplication, including failed tails.
            self.lines.extend(lines)
            try:
                for line in lines:
                    self._observe(line)
            finally:
                if lines:
                    self._publish()
        if (
            self.boot_deadline is not None
            and self.facts.bootstrap is None
            and time.monotonic() >= self.boot_deadline
        ):
            raise TimeoutError("launch-anchored adapter bootstrap deadline exceeded")
        self._ready()
        for name, raw in self.active_documents.items():
            if _read_bytes(self.root / name) != raw:
                raise RuntimeError("active handshake changed/disappeared")
        physical = self.root / PHYSICAL_NAME
        if physical.exists() and read_document(physical).get("status") == "failed":
            raise RuntimeError("physical adapter transition failed")
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("owned logcat exited before detachment")

    def _record_am_result(
        self,
        command: list[str],
        child: subprocess.Popen[bytes],
        output: BinaryIO,
        started: float,
    ) -> None:
        if command[3:6] != ["shell", "am", "start"]:
            return
        output.seek(0)
        raw = output.read(MAX_BYTES + 1)
        phase = (
            "arm"
            if any(arg.endswith("/active-loss/arm") for arg in command)
            else "payload"
            if "android.intent.action.VIEW" in command
            else "launch"
        )
        write_document(
            self.log.with_suffix(f".am-{phase}.json"),
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "phase": phase,
                "command": command,
                "returncode": child.returncode,
                "stdout": raw[:MAX_BYTES].decode("utf-8", errors="replace"),
                "stdout_truncated": len(raw) > MAX_BYTES,
                "started_monotonic": started,
                "finished_monotonic": time.monotonic(),
            },
        )

    def _command(self, *arguments: str) -> str:
        """Pump markers during ADB, so SDK return never extends the ACK deadline."""
        self._poll()
        command = android_sdk.adb_command(self.device, *arguments)
        deadline = min(self.deadline, time.monotonic() + COMMAND_SECONDS)
        if self.sent_at is not None and not self.js_ack:
            deadline = min(deadline, self.sent_at + ACK_SECONDS)
        if self.arm_sent_at is not None and not self.arm_js_ack:
            deadline = min(deadline, self.arm_sent_at + ACK_SECONDS)
        primary: BaseException | None = None
        with tempfile.TemporaryFile() as output:
            self.phone_guard()
            started = time.monotonic()
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
                        raise subprocess.TimeoutExpired(command, COMMAND_SECONDS)
                    time.sleep(POLL_SECONDS)
                self._poll()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, COMMAND_SECONDS)
                output.seek(0)
                raw = output.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise RuntimeError("ADB command output exceeds capture budget")
                text = raw.decode("utf-8", errors="strict")
                if child.returncode != 0:
                    raise subprocess.CalledProcessError(child.returncode, command, text)
                return text
            except BaseException as error:
                primary = error
                raise
            finally:
                try:
                    stop_local(child, console=False)
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note(f"ADB child cleanup: {error}")
                finally:
                    try:
                        self._record_am_result(command, child, output, started)
                    except BaseException as error:
                        if primary is None:
                            raise
                        primary.add_note(f"am stdout receipt: {error}")

    def _installed_uri(self) -> str:
        paths = (
            self._command("shell", "pm", "path", ANDROID_PACKAGE).strip().splitlines()
        )
        if (
            len(paths) != 1
            or re.fullmatch(r"package:/[^\s]+/base\.apk", paths[0]) is None
        ):
            raise RuntimeError("expected one installed Android base.apk sourceDir")
        return "file:" + quote(paths[0][len("package:") :], safe="/:@!$&'()*+,;=")

    def _verify_process(self, boundary: str) -> None:
        self.phone_guard()
        self.guard()
        pid = self._command("shell", "pidof", ANDROID_PACKAGE).strip()
        if self.pid is None or pid != str(self.pid):
            raise RuntimeError("pinned Android PID replaced, ambiguous or absent")
        executable = self._installed_uri()
        if executable != self.executable:
            raise RuntimeError("installed Android sourceDir changed")
        self.receipts.append(
            {"boundary": boundary, "pidof": pid, "executable": executable}
        )
        self.guard()
        self._poll()

    def _launch(self) -> None:
        self.guard()
        self.executable = self._installed_uri()
        self.phone_guard()
        self.output = self.log.with_suffix(".raw.log").open("xb")
        # Start before launch, retaining native bootstrap even before pidof succeeds.
        # One unfiltered stream is permitted; every BLEHUB record is PID-checked.
        self.process = subprocess.Popen(
            android_sdk.adb_command(self.device, "logcat", "-v", "threadtime"),
            stdin=subprocess.DEVNULL,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.boot_deadline = min(self.deadline, time.monotonic() + BOOT_SECONDS)
        output = self._command(
            "shell",
            "am",
            "start",
            "-n",
            COMPONENT,
            "--es",
            "blehub.runtimeMode",
            "rf-adapter-transition",
            "--es",
            "blehub.rfAdapterRunId",
            self.run_id,
            "--es",
            "blehub.rfServiceUuid",
            self.service,
            "--es",
            "blehub.rfExpectedLocalName",
            shlex.quote(self.name),
            "--es",
            "blehub.rfAdapterMode",
            self.mode,
        )
        self._check_launch(output)
        self.pid = int(
            wait_for_initial_pid(
                self.device,
                self.log,
                max(0.0, min(self.deadline, self.boot_deadline) - time.monotonic()),
                guard=self.guard,
            )
        )
        self.stream = PrefixStream(
            self.log.with_suffix(".raw.log"), self.pid, prefix=RAW_PREFIX
        )
        self._verify_process("launch")

    @staticmethod
    def _check_launch(output: str, *, control: bool = False) -> None:
        lines = output.splitlines()
        if control and lines.count(DELIVERED_WARNING) == 1:
            lines.remove(DELIVERED_WARNING)
        if any(
            token in line
            for line in lines
            for token in ("Warning", "Error", "Exception", "Activity not started")
        ):
            raise RuntimeError(f"Android adapter launch/control rejected: {output}")

    def _send_on(self) -> None:
        if self.facts.stage != 7 or self.sent_at is not None:
            raise RuntimeError("adapter ON control schedule drift")
        if not self._ready():
            raise RuntimeError("adapter ON requires the provider ready ACK")
        self._verify_process("before-radio-on")
        self._poll()
        self.phone_guard()
        self.radio_on()  # Real svc/readback; separate from the URL ACK clock.
        self.on_confirmed = True
        self._poll()  # Reject unsolicited ACKs emitted during the radio operation.
        self._verify_process("after-radio-on")
        self._poll()
        self.sent_at = time.monotonic()
        output = self._command(
            "shell",
            "am",
            "start",
            "--activity-single-top",
            "-n",
            COMPONENT,
            "-a",
            "android.intent.action.VIEW",
            "-d",
            signal_url(self.run_id, self.mode, "on"),
        )
        self._check_launch(output, control=True)
        self._verify_process("after-url")

    def _active_document(
        self, name: str, phase: str, extra: set[str]
    ) -> dict[str, Any] | None:
        path = self.root / name
        if not path.exists() and not path.is_symlink():
            return None
        raw = _read_bytes(path)
        value = _json(raw)
        base = {
            "schema_version",
            "adapter_mode",
            "run_id",
            "phase",
            "receipt_sha256",
            "native_bootstrap",
        }
        if (
            set(value) != base | extra
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
            or value.get("adapter_mode") != "active-loss"
            or value.get("run_id") != self.run_id
            or value.get("phase") != phase
            or value.get("receipt_sha256") != self.receipt_sha256
            or value.get("native_bootstrap") != self.facts.bootstrap
        ):
            raise RuntimeError("foreign/malformed active-loss handshake")
        peer = value.get("peer")
        try:
            ticket = ticket_from_document(value["ticket"])
            status = status_from_document(value["status"])
        except ValueError as error:
            raise RuntimeError("invalid active v2 fault receipt") from error
        if (
            ticket.owner_nonce != owner_nonce(self.run_id)
            or ticket.operation is not Operation.READ
            or status.ticket != ticket
            or status.phase is not (Phase.ARMED if phase == "armed" else Phase.HELD)
            or status.before != b"\x00"
            or status.after != b"\x00"
            or type(peer) is not list
            or len(peer) != 2
            or type(peer[0]) is not str
            or re.fullmatch(r"[0-9a-f]{12}", peer[0]) is None
            or type(peer[1]) is not int
            or peer[1] not in range(4)
        ):
            raise RuntimeError("invalid active v2 ticket/status/peer")
        if status.peer != bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1]:
            raise RuntimeError("active STATUS/GAP peer drift")
        armed = self.active_documents.get(ARM_NAME)
        if armed is not None:
            prior = _json(armed)
            if any(value.get(k) != prior.get(k) for k in ("ticket", "peer")):
                raise RuntimeError("active-loss physical generation changed")
        if "route" in extra and value["route"] != self.facts.active.discovery:
            raise RuntimeError("active-loss JS/native route drift")
        if name in self.active_documents and self.active_documents[name] != raw:
            raise RuntimeError("active handshake changed")
        self.active_documents[name] = raw
        return value

    def _active_actions(self) -> None:
        active = self.facts.active
        if active is None:
            return
        if active.discovery is not None and self.arm_sent_at is None:
            value = self._active_document(
                ARM_NAME, "armed", {"ticket", "status", "peer", "route"}
            )
            if value is None:
                return
            self._verify_process("before-arm")
            active.arm_authorized = True
            self.arm_sent_at = time.monotonic()
            output = self._command(
                "shell",
                "am",
                "start",
                "--activity-single-top",
                "-n",
                COMPONENT,
                "-a",
                "android.intent.action.VIEW",
                "-d",
                signal_url(self.run_id, self.mode, "arm"),
            )
            self._check_launch(output, control=True)
        if active.accepted == ["32008", "32009"] and self.off_requested_at is None:
            if (
                self._active_document(
                    LOSS_NAME, "waiting", {"ticket", "status", "peer", "route"}
                )
                is None
            ):
                return
            self._verify_process("before-radio-off")
            self.off_requested_at = time.monotonic()
            write_document(
                self.root / OFF_REQUEST,
                {
                    "schema_version": 1,
                    "adapter_mode": self.mode,
                    "run_id": self.run_id,
                    "loss_ready_sha256": hashlib.sha256(
                        self.active_documents[LOSS_NAME]
                    ).hexdigest(),
                },
            )
            self.active_documents[OFF_REQUEST] = _read_bytes(self.root / OFF_REQUEST)
        if self.off_requested_at is not None and self.off_started_at is None:
            if time.monotonic() - self.off_requested_at >= 1:
                raise TimeoutError(
                    "fresh HELD revalidation ACK exceeded 1s; refusing OFF"
                )
            value = self._active_document(
                OFF_READY,
                "off-authorized",
                {"ticket", "status", "peer", "request_sha256"},
            )
            if value is None:
                return
            if (
                value["request_sha256"]
                != hashlib.sha256(self.active_documents[OFF_REQUEST]).hexdigest()
            ):
                raise RuntimeError("OFF ACK does not bind this fresh request")
            self._poll()  # Terminal before actual injection must fail, not be adopted.
            self.phone_guard()
            self.guard()
            active.loss_authorized = True
            self.off_started_at = time.monotonic()
            assert self.radio_off is not None
            self.radio_off(
                timeout=OFF_CALL_SECONDS
            )  # Single mutation; cleanup has its own budget.
            self.off_finished_at = time.monotonic()
            if self.off_finished_at - self.off_started_at >= OFF_CALL_SECONDS:
                raise TimeoutError(
                    "OFF readback exceeded the 4s pre-watchdog test budget"
                )
            write_document(
                self.root / OFF_CONFIRMED,
                {
                    "schema_version": 1,
                    "adapter_mode": self.mode,
                    "run_id": self.run_id,
                    "off_ready_sha256": hashlib.sha256(
                        self.active_documents[OFF_READY]
                    ).hexdigest(),
                },
            )
            self.active_documents[OFF_CONFIRMED] = _read_bytes(
                self.root / OFF_CONFIRMED
            )
            self._poll()

    def _physical_passed(self) -> bool:
        path = self.root / PHYSICAL_NAME
        if not path.exists():
            return False
        value = read_document(path)
        if (
            value.get("schema_version") != 1
            or value.get("mode") != MODE
            or value.get("run_id") != self.run_id
            or value.get("status") != "passed"
        ):
            raise RuntimeError("foreign/failed physical adapter proof")
        return True

    def _cleanup_app(self) -> None:
        # Never select a replacement PID for cleanup, even after a failed launch.
        if self.pid is not None:
            _stop_owned_app(self.device, self.pid, self.executable, self.phone_guard)

    def run(self) -> str:
        primary: BaseException | None = None
        try:
            self._launch()
            while True:
                self._poll()
                if time.monotonic() >= self.next_identity:
                    self._verify_process("periodic")
                    self.next_identity = time.monotonic() + PROCESS_SECONDS
                self._active_actions()
                if self.facts.stage == 7 and self.sent_at is None and self._ready():
                    self._send_on()
                if self.facts.passed and self._physical_passed():
                    break
                time.sleep(POLL_SECONDS)
            assert self.process is not None
            stop_local(self.process, console=False)
            self.process = None
            self._poll()  # Include every complete late record written before detach.
            assert self.stream is not None
            if RAW_PREFIX.encode() in self.stream.previous[self.stream.complete :]:
                raise RuntimeError("truncated final BLEHUB runtime record")
            self._verify_process("after-logcat-detach")
            self._identity("passed")
            return self.text()
        except BaseException as error:
            primary = error
            for label, action in (
                ("abort", lambda: (self.root / "abort").write_text("capture-failed\n")),
                ("identity", lambda: self._identity("failed")),
                ("owned app cleanup", self._cleanup_app),
            ):
                try:
                    action()
                except BaseException as cleanup:  # noqa: BLE001 - preserve primary during owned cleanup
                    error.add_note(f"{label}: {cleanup}")
            raise
        finally:
            try:
                if self.process is not None:
                    stop_local(self.process, console=False)
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"logcat cleanup: {error}")
            finally:
                if self.output is not None:
                    self.output.close()


def capture(
    device: str,
    log: Path,
    coordination: Path,
    run_id: str,
    service_uuid: str,
    local_name: str,
    *,
    timeout: float,
    guard: Callable[[], None],
    radio_on: Callable[[], None],
    phone_guard: Callable[[], None] = lambda: None,
    mode: str = "startup-off",
    radio_off: Callable[..., None] | None = None,
) -> str:
    return AdapterCapture(
        device,
        log,
        coordination,
        run_id,
        service_uuid,
        local_name,
        timeout=timeout,
        guard=guard,
        radio_on=radio_on,
        phone_guard=phone_guard,
        mode=mode,
        radio_off=radio_off,
    ).run()
