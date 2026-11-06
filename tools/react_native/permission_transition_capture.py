"""Three fresh Android phases, one bounded logcat child, no radio mutations.

The parent owns installation, fixture and phone leases. This owner uses the
shared pumped CaptureTransport for commands, PrefixStream for append-only bytes,
and the provider RuntimeFacts for wire semantics. No retry or inferred death.
"""

from __future__ import annotations

import math
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tests.peripherals.nrf52840 import permission_transition as wire
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fixture_session import _json, _read_bytes
from tools.android.permission_control import AndroidPermissionController, _start_ticks
from tools.react_native import android_sdk
from tools.react_native import permission_transition_ledger as ledger
from tools.react_native.android_rf_host import wait_for_initial_pid
from tools.react_native.capture_io import THREADTIME, PrefixStream, stop_local
from tools.react_native.capture_transport import (
    ANDROID_COMPONENT,
    BOOT_SECONDS,
    COMMAND_SECONDS,
    POLL_SECONDS,
    PROCESS_SECONDS,
    CaptureTransport,
)
from tools.react_native.host_contract import ANDROID_PACKAGE

RAW_NAME = "permission-transition-runtime.raw.log"
RESULT_NAME = "permission-transition-runtime.result.json"
PROOF_NAME = "permission-transition-os-death-proof.json"
OS_RAW_NAME = "permission-transition-exit-info.raw.log"
SILENCE_SECONDS = 0.3
ACK_SECONDS = 2.0
DELIVERED_WARNING = "Warning: Activity not started, intent has been delivered to currently running top-most instance."


def validate_inputs(device: str, timeout: float) -> None:
    if (
        type(device) is not str
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", device) is None
        or device.startswith("-")
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValueError(
            "explicit Android device and finite timeout in (0, 300] required"
        )


class PermissionCapture(CaptureTransport):
    """Own the stream, not the phone/fixture. result is durable even on failure."""

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
        permission: AndroidPermissionController,
        phone_guard: Callable[[], None],
    ) -> None:
        validate_inputs(device, timeout)
        if (
            any(str(uuid.UUID(v)) != v for v in (run_id, service_uuid))
            or not local_name.strip()
            or len(local_name) > 512
        ):
            raise ValueError("canonical UUIDs and bounded fixture local name required")
        super().__init__(
            "android",
            device,
            log,
            coordination,
            run_id,
            service_uuid,
            local_name,
            timeout,
            guard,
            None,
        )
        self.permission, self.phone_guard = permission, phone_guard
        self.facts = wire.RuntimeFacts(run_id, service_uuid, local_name)
        self.documents: dict[str, bytes] = {}
        self.receipt_raw = _read_bytes(
            self.root / "fixture-session/fixture-receipt.json"
        )
        self.receipt_sha256 = ledger.digest(self.receipt_raw)
        self.phase: str | None = None
        self.launching = False
        self.arm_at: float | None = None
        self.death_window = False
        self.cleaning = False
        self.next_identity = self.started
        self.result: dict[str, Any] = {
            "schema_version": 1,
            "platform": "android",
            "scenario": wire.MODE,
            "run_id": run_id,
            "device": device,
            "status": "running",
            "phases": {},
            "boundaries": [],
            "commands": [],
            "raw_log": RAW_NAME,
            "runtime_log": log.name,
            "coordination_runtime_log": f"coordination/{wire.RUNTIME_NAME}",
            "os_journal": permission.path.name,
            "os_death_proof": None,
            "os_raw_proof": None,
            "primary_error": None,
        }
        self.result_path = log.parent / RESULT_NAME

    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def _publish(self) -> None:
        for path in (self.log, self.root / wire.RUNTIME_NAME):
            temporary = path.with_name(path.name + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    stream.write(self.text())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        write_document(self.result_path, self.result)

    def _command(self, command: list[str], *, budget: float = COMMAND_SECONDS) -> str:
        self.phone_guard()
        mutation = any(
            token in command[3:] for token in ("force-stop", "kill", "install", "start")
        )
        if self.death_window and mutation:
            raise RuntimeError(
                "host process mutation forbidden in revoke/death-proof window"
            )
        ledger.require(
            len(self.result["commands"]) < 4096,
            "capture command receipt budget exceeded",
        )
        started = time.monotonic()
        row: dict[str, Any] = {
            "command": command,
            "phase": self.phase,
            "started_monotonic": started,
            "timeout_seconds": min(budget, max(0.0, self.deadline - started)),
        }
        self.result["commands"].append(row)
        try:
            output = super()._command(command, budget=budget)
            ledger.require(
                len(output.encode()) <= 65536, "capture command stdout exceeds 64 KiB"
            )
            row.update(stdout=output, returncode=0)
            return output
        except BaseException as error:
            row["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            row["finished_monotonic"] = time.monotonic()
            write_document(self.result_path, self.result)

    def _adb(self, *args: str) -> str:
        return self._command(android_sdk.adb_command(self.device, *args))

    def _request_deadline(self) -> float:
        if self.arm_at is not None and not self.facts.js_arm:
            return min(self.deadline, self.arm_at + ACK_SECONDS)
        return self.deadline

    def _poll(self) -> None:
        if self.cleaning:
            self.phone_guard()
            return
        now = time.monotonic()
        if now >= self.deadline:
            raise TimeoutError("permission capture absolute deadline exceeded")
        if (
            self.arm_at is not None
            and not self.facts.js_arm
            and now >= self.arm_at + ACK_SECONDS
        ):
            raise TimeoutError("native/JS ARM acknowledgment exceeded 2s")
        if now >= self.next_guard:
            self.phone_guard()
            self.guard()
            if (
                _read_bytes(self.root / "fixture-session/fixture-receipt.json")
                != self.receipt_raw
            ):
                raise RuntimeError("fixture receipt changed")
            self.next_guard = now + PROCESS_SECONDS
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("owned permission logcat exited early")
        for name, raw in self.documents.items():
            if _read_bytes(self.root / name) != raw:
                raise RuntimeError("immutable permission handshake changed/disappeared")
        if self.stream is not None and not self.launching:
            incoming = self.stream.poll()
            self.lines.extend(
                incoming
            )  # Preserve the rejected batch and raw order too.
            try:
                for line in incoming:
                    self._observe(line)
            finally:
                if incoming:
                    self._publish()
        physical = self.root / wire.PHYSICAL_NAME
        if physical.exists() and read_document(physical).get("status") == "failed":
            raise RuntimeError("permission provider failed")
        if (
            self.phase
            and self.phase not in self.facts.bootstraps
            and now >= self.boot_deadline
        ):
            raise TimeoutError("native permission bootstrap deadline exceeded")

    def _observe(self, line: str) -> None:
        header = THREADTIME.match(line)
        ledger.require(
            header is not None and line.count(wire.PREFIX) == 1,
            "foreign source PID/header/prefix",
        )
        marker = line.split(wire.PREFIX, 1)[1]
        source = int(header[1])
        known = {entry["pid"]: phase for phase, entry in self.result["phases"].items()}
        ledger.require(source in known, "runtime source PID not owned")
        if marker.startswith("native:bootstrap:"):
            value = _json(marker.split(":", 2)[2].encode())
            entry = self.result["phases"][self.phase]
            ledger.require(
                source == self.pid == value.get("pid")
                and value.get("phase") == self.phase
                and value.get("uid") == self.permission.uid
                and value.get("user_id") == 0
                and value.get("executable") == self.executable
                and time.monotonic() < self.boot_deadline,
                "native bootstrap differs from pinned PID/UID/user/sourceDir",
            )
            self.facts.consume(line)
            entry["native_bootstrap"] = value
            entry["process_identity"]["native_started_unix_ms"] = value[
                "started_unix_ms"
            ]
        else:
            # Already-exited phase tails are read in original order, never relabeled
            # as the new phase. RuntimeFacts rejects true late/duplicate callbacks.
            ledger.require(
                known[source] == self.facts.phase,
                "late old-PID marker crossed native phase boundary",
            )
            self.facts.consume(line)
        if self.facts.phase in self.facts.nonces:
            self.result["phases"][self.facts.phase]["nonce"] = self.facts.nonces[
                self.facts.phase
            ]

    def _uri(self) -> str:
        paths = self._adb("shell", "pm", "path", ANDROID_PACKAGE).strip().splitlines()
        ledger.require(
            len(paths) == 1
            and re.fullmatch(r"package:/[^\s]+/base\.apk", paths[0]) is not None,
            "one installed base.apk required",
        )
        return "file:" + quote(paths[0][len("package:") :], safe="/:@!$&'()*+,;=")

    def _pin(self, boundary: str) -> dict[str, Any]:
        ledger.require(self.pid is not None, "cannot own an unpinned app")
        ledger.require(
            self._adb("shell", "pidof", ANDROID_PACKAGE).strip() == str(self.pid)
            and self._uri() == self.executable,
            "pinned PID/sourceDir changed",
        )
        ticks = _start_ticks(
            self._adb("shell", "cat", f"/proc/{self.pid}/stat"), self.pid
        )
        status = self._adb("shell", "cat", f"/proc/{self.pid}/status")
        ledger.require(
            re.findall(r"(?m)^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", status)
            == [(str(self.permission.uid),) * 4],
            "pinned process UID mismatch",
        )
        ledger.require(
            self._adb("shell", "cat", f"/proc/{self.pid}/cmdline").rstrip("\0")
            == ANDROID_PACKAGE,
            "pinned process cmdline mismatch",
        )
        ledger.require(
            _start_ticks(self._adb("shell", "cat", f"/proc/{self.pid}/stat"), self.pid)
            == ticks
            and self._adb("shell", "pidof", ANDROID_PACKAGE).strip() == str(self.pid),
            "PID/procstart changed during pin",
        )
        identity = {
            "pid": self.pid,
            "uid": self.permission.uid,
            "user_id": 0,
            "process_name": ANDROID_PACKAGE,
            "start_ticks": ticks,
            "native_started_unix_ms": self.facts.bootstraps.get(self.phase, {}).get(
                "started_unix_ms", 0
            ),
        }
        existing = self.result["phases"].get(self.phase)
        if existing:
            ledger.require(
                identity == existing["process_identity"],
                "pinned OS/native identity changed",
            )
            existing["process_receipts"].append(
                {
                    "boundary": boundary,
                    "identity": identity,
                    "monotonic": time.monotonic(),
                }
            )
        self.next_identity = time.monotonic() + PROCESS_SECONDS
        return identity

    def _require_absent(self) -> None:
        self.phone_guard()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no budget for fresh-process absence check")
        result = android_sdk.run_command(
            android_sdk.adb_command(self.device, "shell", "pidof", ANDROID_PACKAGE),
            timeout=min(1.0, remaining),
            check=False,
        )
        self._poll()
        ledger.require(
            result.returncode == 1 and not result.stdout.strip(),
            "fresh launch refuses an existing/ambiguous app",
        )

    @staticmethod
    def _check_launch(output: str, *, arm: bool = False) -> None:
        lines = output.splitlines()
        if arm and lines.count(DELIVERED_WARNING) == 1:
            lines.remove(DELIVERED_WARNING)
        ledger.require(
            not any(
                token in line
                for line in lines
                for token in ("Warning", "Error", "Exception", "Activity not started")
            ),
            "launch/control warning/error",
        )

    def _launch_phase(self, phase: str) -> None:
        ledger.require(
            not self.death_window and phase not in self.result["phases"],
            "duplicate/unsafe phase launch",
        )
        self._require_absent()
        self.phase, self.pid = phase, None
        uri = self._uri()
        ledger.require(
            not self.executable or self.executable == uri,
            "installed sourceDir drift between phases",
        )
        self.executable = uri
        self.boot_deadline = min(self.deadline, time.monotonic() + BOOT_SECONDS)
        self.launching = True
        try:
            output = self._adb(
                "shell",
                "am",
                "start",
                "-n",
                ANDROID_COMPONENT,
                "--es",
                "blehub.runtimeMode",
                "rf-permission-transition",
                "--es",
                "blehub.rfPermissionRunId",
                self.run_id,
                "--es",
                "blehub.rfPermissionPhase",
                phase,
                "--es",
                "blehub.rfServiceUuid",
                self.service,
                "--es",
                "blehub.rfExpectedLocalName",
                shlex.quote(self.name),
            )
            self._check_launch(output)
            phase_log = self.log.with_name(f"permission-transition-{phase}.log")
            self.pid = int(
                wait_for_initial_pid(
                    self.device,
                    phase_log,
                    max(0.0, min(self.deadline, self.boot_deadline) - time.monotonic()),
                    guard=self.phone_guard,
                )
            )
            ledger.require(
                all(
                    self.pid != entry["pid"] for entry in self.result["phases"].values()
                ),
                "phase reused an old PID",
            )
            identity = self._pin("launch")
            self.result["phases"][phase] = {
                "pid": self.pid,
                "pid_receipt": phase_log.with_suffix(".identity.json").name,
                "process_identity": identity,
                "native_bootstrap": None,
                "nonce": None,
                "process_receipts": [],
                "retired": False,
                "dead": False,
                "closed_resources": None,
            }
        finally:
            self.launching = False
        self._poll()

    def _wait(
        self,
        ready: Callable[[], bool],
        label: str,
        *,
        deadline: float | None = None,
        verify_pid: bool = True,
    ) -> None:
        end = min(self.deadline, deadline if deadline is not None else self.deadline)
        while True:
            self._poll()
            if time.monotonic() >= end:
                raise TimeoutError(f"permission {label} deadline exceeded")
            if ready():
                return
            if (
                verify_pid
                and self.pid is not None
                and self.phase in self.facts.bootstraps
                and time.monotonic() >= self.next_identity
            ):
                self._pin(label)
            time.sleep(min(POLL_SECONDS, max(0.0, end - time.monotonic())))

    def _silence(self) -> None:
        # This is a minimum observation interval, not an ADB completion deadline.
        # Identity checks keep pumping the stream and may span several round trips;
        # the unchanged absolute capture deadline still bounds the whole wait.
        until = time.monotonic() + SILENCE_SECONDS
        self._wait(lambda: time.monotonic() >= until, "phase silence")

    def _boundary(self, kind: str) -> None:
        getattr(self.facts, kind)()
        self.result["boundaries"].append(
            {
                "kind": kind,
                "after_marker": len(self.lines),
                "monotonic": time.monotonic(),
            }
        )
        self._publish()

    def _document(self, name: str) -> dict[str, Any] | None:
        path = self.root / name
        if not path.exists() and not path.is_symlink():
            return None
        observed = time.monotonic()
        raw = _read_bytes(path)
        if name in self.documents:
            ledger.require(raw == self.documents[name], "changed permission handshake")
        else:
            self.documents[name] = raw
            if name == wire.REVOKE_AUTHORIZED_NAME:
                self.result["authorized_monotonic"] = observed
        return _json(raw)

    def _ready(self, name: str) -> bool:
        value = self._document(name)
        if value is None:
            return False
        ledger.verify_readiness(
            value,
            name,
            facts=self.facts,
            receipt_sha256=self.receipt_sha256,
            armed=_json(self.documents[wire.ARM_READY_NAME])
            if wire.ARM_READY_NAME in self.documents
            else None,
            request_raw=self.documents.get(wire.REVOKE_REQUEST_NAME),
            ready_raw=self.documents.get(wire.REVOKE_READY_NAME),
        )
        return True

    def _host_document(self, name: str, **fields: Any) -> None:
        path = self.root / name
        ledger.require(
            not path.exists() and not path.is_symlink(),
            "stale host permission document",
        )
        write_document(path, {"schema_version": 1, "run_id": self.run_id, **fields})
        self.documents[name] = _read_bytes(path)

    def stop_owned_app(self) -> None:
        """Failure-only exact-instance recovery, after proof collection and restore."""
        ledger.require(
            not self.death_window,
            "host force forbidden before death proof collection ends",
        )
        if self.phase is None:
            return
        entry = self.result["phases"].get(self.phase)
        if (
            not entry
            or entry["native_bootstrap"] is None
            or entry["dead"]
            or entry["retired"]
        ):
            return  # Never adopt an app without a native identity and PID receipt.
        self.phone_guard()
        # Cleanup has a separate bounded budget, but never a new identity selection.
        old_deadline, old_stream = self.deadline, self.stream
        self.deadline, self.stream = time.monotonic() + 20, None
        self.cleaning = True
        try:
            self._pin("failure-retire")
            self._adb("shell", "am", "force-stop", ANDROID_PACKAGE)
            entry["retired"] = True
        finally:
            self.deadline, self.stream = old_deadline, old_stream
            self.cleaning = False

    def _retire_denied(self) -> None:
        self._silence()
        self._pin("denied-retire")
        self._adb("shell", "am", "force-stop", ANDROID_PACKAGE)
        self.result["phases"]["denied"].update(
            retired=True, closed_resources=self.facts.resources["denied"]
        )
        self.pid = None
        self._require_absent()
        self._poll()

    def _persist_os_proof(self) -> None:
        """Retain original controller bytes even on a failed/unknown death gate."""
        raw = _read_bytes(self.permission.path)
        path = self.log.parent / PROOF_NAME
        with path.open("xb") as stream:
            stream.write(raw)
        self.result["os_death_proof"] = path.name
        journal = _json(raw)
        dumps = [
            r["stdout"]
            for a in journal["actions"]
            if a["label"] == "await-permission-death"
            for r in a["receipts"]
            if r["command"][4:] == ["dumpsys", "activity", "exit-info", ANDROID_PACKAGE]
            and r.get("stdout_complete") is True
        ]
        if dumps:
            path = self.log.parent / OS_RAW_NAME
            with path.open("xb") as stream:
                stream.write(dumps[-1].encode())
            self.result["os_raw_proof"] = path.name
        self._publish()

    def _revoke(self) -> None:
        authorized = self._document(wire.REVOKE_AUTHORIZED_NAME)
        ledger.require(authorized is not None, "missing revoke authorization")
        at = self.result["authorized_monotonic"]
        self._poll()  # Reject any queued/watchdog callback before the irreversible action.
        identity = self.result["phases"]["active"]["process_identity"]
        self.death_window = True
        original_guard = self.permission.phone_guard
        primary: BaseException | None = None
        # The mature controller pumps this capture before every bounded OS command.
        self.permission.phone_guard = lambda: (self.phone_guard(), self._poll())
        try:
            budget = min(
                4.0, at + 4 - time.monotonic(), self.deadline - time.monotonic()
            )
            if budget <= 0:
                raise TimeoutError("authorization-to-revoke budget exhausted")
            self.permission.revoke_connected(identity, timeout=budget)
            ledger.require(
                self.permission.revoke_monotonic is not None,
                "controller lacks revoke clock",
            )
            self.result["revoked_elapsed_ms"] = int(
                (self.permission.revoke_monotonic - at) * 1000
            )
            remaining = min(
                6.0,
                self.permission.revoke_monotonic + 6 - time.monotonic(),
                self.deadline - time.monotonic(),
            )
            if remaining <= 0:
                raise TimeoutError("total revoke-to-death budget exhausted")
            self.permission.await_permission_death(identity, timeout=remaining)
            self._poll()
        except BaseException as error:
            primary = error
            raise
        finally:
            self.permission.phone_guard = original_guard
            try:
                self._persist_os_proof()
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"original OS proof persistence failed: {error}")
            finally:
                self.death_window = False
        proof = _read_bytes(self.log.parent / PROOF_NAME)
        os_raw = _read_bytes(self.log.parent / OS_RAW_NAME)
        record = ledger.verify_death(
            proof,
            os_raw,
            identity=identity,
            run_id=self.run_id,
            device=self.device,
            authorized_at=at,
        )
        self.result["phases"]["active"].update(dead=True, closed_resources=None)
        self._host_document(
            wire.LOSS_CONFIRMED_NAME,
            authorized_sha256=ledger.digest(
                self.documents[wire.REVOKE_AUTHORIZED_NAME]
            ),
            request_nonce=authorized["request_nonce"],
            pid=record["pid"],
            uid=record["real_uid"],
            user_id=record["user_id"],
            reason=8,
            exit_proof_sha256=ledger.digest(proof),
            revoked_elapsed_ms=self.result["revoked_elapsed_ms"],
        )
        self._boundary("confirm_loss")
        self.pid = None

    def run(self) -> str:
        # Reject stale evidence before entering any publishing/finalization scope.
        for path in (
            self.log,
            self.log.parent / RAW_NAME,
            self.log.parent / PROOF_NAME,
            self.log.parent / OS_RAW_NAME,
            self.result_path,
            self.root / wire.RUNTIME_NAME,
            *(
                self.log.parent / f"permission-transition-{phase}.identity.json"
                for phase in wire.PHASES
            ),
        ):
            ledger.require(
                not path.exists() and not path.is_symlink(),
                "stale permission capture output",
            )
        primary: BaseException | None = None
        try:
            ledger.require(
                self.permission.initialized
                and self.permission.run_id == self.run_id
                and self.permission.device == self.device,
                "initialized same-run permission controller required",
            )
            self.phone_guard()
            self.output = (self.log.parent / RAW_NAME).open("xb")
            command = android_sdk.adb_command(
                self.device, "logcat", "-b", "main", "-v", "threadtime"
            )
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.stream = PrefixStream(
                self.log.parent / RAW_NAME, None, prefix=ledger.RAW_PREFIX, raw=True
            )
            self.result.update(capture_pid=self.process.pid, capture_command=command)
            self.permission.deny_startup()
            self._launch_phase("denied")
            self._wait(lambda: "denied" in self.facts.passes, "denied")
            self.permission.grant(label="active")
            self._retire_denied()
            self._host_document(wire.ACTIVE_GO_NAME, phase="active")
            self._boundary("authorize_active")
            self._launch_phase("active")
            self._wait(
                lambda: (
                    self.facts.route is not None and self._ready(wire.ARM_READY_NAME)
                ),
                "active discovery/arm-ready",
            )
            self._pin("before-arm")
            self._boundary("authorize_arm")
            self.arm_at = time.monotonic()
            output = self._adb(
                "shell",
                "am",
                "start",
                "--activity-single-top",
                "-n",
                ANDROID_COMPONENT,
                "-a",
                "android.intent.action.VIEW",
                "-d",
                wire.arm_url(self.run_id),
            )
            self._check_launch(output, arm=True)
            self._wait(
                lambda: self.facts.pending and self._ready(wire.REVOKE_READY_NAME),
                "native pending2/watch1 + exact WAITING",
            )
            self._boundary("mark_waiting")
            self._host_document(
                wire.REVOKE_REQUEST_NAME,
                route=self.facts.route,
                ready_sha256=ledger.digest(self.documents[wire.REVOKE_READY_NAME]),
                request_nonce=str(uuid.uuid4()),
            )
            self._wait(
                lambda: self._ready(wire.REVOKE_AUTHORIZED_NAME),
                "fresh revoke challenge",
                deadline=time.monotonic() + 1,
                verify_pid=False,
            )
            self._revoke()
            self._wait(
                lambda: self._ready(wire.RESTORED_READY_NAME),
                "provider restored-ready",
                verify_pid=False,
            )
            self._boundary("authorize_restored")
            self.permission.grant(label="restored")
            self._launch_phase("restored")
            self._wait(lambda: self.facts.passed, "restored rf-io")
            self.result["phases"]["restored"]["closed_resources"] = (
                self.facts.resources["restored"]
            )
            self._silence()
            self._pin("restored-complete")
            self._wait(
                lambda: (self.root / wire.PHYSICAL_NAME).exists(), "physical closed"
            )
            self.result["status"] = "passed"
            self._publish()
            return self.text()
        except BaseException as error:
            primary = error
            self.result.update(
                status="failed", primary_error=f"{type(error).__name__}: {error}"
            )
            raise
        finally:
            try:
                if self.process is not None:
                    stop_local(self.process, console=False)
                if self.stream is not None:
                    # Final drain keeps late bytes visible; a late callback still fails.
                    incoming = self.stream.poll()
                    self.lines.extend(incoming)
                    for line in incoming:
                        self._observe(line)
                self._publish()
            except BaseException as error:
                self.result.update(status="failed")
                try:
                    self._publish()
                except (OSError, RuntimeError, ValueError) as publication:
                    error.add_note(f"failed result publication: {publication}")
                if primary is None:
                    raise
                primary.add_note(f"capture detach/publication: {error}")
            finally:
                if self.output is not None:
                    self.output.close()


def capture(*args: Any, **kwargs: Any) -> PermissionCapture:
    """Return the completed owner; .result holds paths/phases for the parent bundle."""
    owner = PermissionCapture(*args, **kwargs)
    owner.run()
    return owner
