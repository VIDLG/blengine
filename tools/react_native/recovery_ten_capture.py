"""Launch/stream/control evidence for an already installed, attested recovery run.

This module owns only local capture children and ten payload URL requests. It
neither installs artifacts nor owns the Peripheral. A passed capture is not an
RF verdict: the parent must run its independent ledger/physical pure checker.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fixture_session import read_live_document
from tests.peripherals.nrf52840.recovery_ten import (
    ACK_SECONDS,
    MODE,
    PHYSICAL_NAME,
    PREFIX,
    PROGRESS_NAME,
    PROGRESS_PHASES,
    STATUSES,
    RuntimeFacts,
)
from tools.react_native.capture_io import PrefixStream, stop_local
from tools.react_native.capture_transport import (
    COMMAND_SECONDS,
    POLL_SECONDS,
    PROCESS_SECONDS,
    CaptureTransport,
)
from tools.react_native.host_contract import (
    ANDROID_PACKAGE,
    BUNDLE_ID,
)


class _Capture(CaptureTransport):
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
        super().__init__(
            platform,
            device,
            log,
            coordination,
            run_id,
            service_uuid,
            local_name,
            timeout,
            guard,
            install_json,
        )
        self.facts = RuntimeFacts(run_id, service_uuid)
        self.actions: list[int] = []
        self.native_acks: list[int] = []
        self.js_acks: list[int] = []
        self.sent_at: float | None = None
        self.pending: int | None = None
        self.progress_cycle = -1
        self.next_heartbeat = self.started
        self.bootstrap_verified = False
        self.receipt["budgets_seconds"]["ack"] = ACK_SECONDS

    def _identity_budget(self, boundary: str) -> float:
        budget = COMMAND_SECONDS
        if boundary.endswith("-after") and self.sent_at is not None:
            budget = min(budget, self.sent_at + ACK_SECONDS - time.monotonic())
            if budget <= 0:
                raise TimeoutError("post-relay identity exceeded original 2s boundary")
        return budget

    def _record(self) -> None:
        self.identity.update(
            bootstrap=self.facts.bootstrap,
            pid=self.pid,
            executable=self.executable,
            device_identifier=self.device_identifier,
            native_acknowledged=list(self.native_acks),
            js_acknowledged=list(self.js_acks),
            launch_started_at=self.receipt.get("launch_started_at"),
            bootstrap_deadline=self.boot_deadline,
        )
        self.receipt.update(
            host_cycle_actions=list(self.actions),
            latest_progress=self.progress,
            normalized_lines=len(self.lines),
        )
        write_document(self.log.with_suffix(".identity.json"), self.identity)
        write_document(self.log.with_suffix(".capture.json"), self.receipt)

    def _request_deadline(self) -> float:
        return (
            self.sent_at + ACK_SECONDS
            if self.pending is not None and self.sent_at is not None
            else self.deadline
        )

    def _ack_deadline(self) -> None:
        if (
            self.pending is not None
            and self.sent_at is not None
            and time.monotonic() >= self.sent_at + ACK_SECONDS
        ):
            raise TimeoutError(
                "native/JS recovery ACK exceeded the original 2s deadline"
            )

    def _observe(self, line: str) -> None:
        marker = line[len(PREFIX) :]
        if marker.startswith("native:bootstrap:"):
            record = json.loads(marker.removeprefix("native:bootstrap:"))
            bundle = ANDROID_PACKAGE if self.platform == "android" else BUNDLE_ID
            if (
                not isinstance(record, dict)
                or record.get("source") != "launch"
                or record.get("bundle_id") != bundle
                or record.get("executable") != self.executable
                or self.pid is not None
                and record.get("pid") != self.pid
            ):
                raise RuntimeError(
                    "foreign native bootstrap package/PID/executable/source"
                )
            if time.monotonic() >= self.boot_deadline:
                raise TimeoutError("launch-anchored native bootstrap deadline exceeded")
            self.facts.observe(line)  # Exact six fields, positive integer PID, UUID.
            self.pid = record["pid"]
            self.identity["status"] = "pinned"
        elif marker.startswith("native:signal:"):
            self._ack_deadline()
            record = json.loads(marker.removeprefix("native:signal:"))
            expected = {
                **(self.facts.bootstrap or {}),
                "source": "payload-url",
                "cycle": self.pending,
                "url": self.facts.url(self.pending)
                if self.pending is not None
                else None,
            }
            if (
                self.pending is None
                or record != expected
                or len(self.native_acks) != self.pending
            ):
                raise RuntimeError(
                    "foreign, early, duplicate or out-of-order native ACK"
                )
            self.facts.observe(line)
            self.native_acks.append(self.pending)
        elif re.match(r"cycle:[0-9]:recovery-ready:", marker):
            self._ack_deadline()
            if (
                self.pending is None
                or self.native_acks != list(range(self.pending + 1))
                or len(self.js_acks) != self.pending
            ):
                raise RuntimeError("early/duplicate JS ACK or JS ACK before native ACK")
            self.facts.observe(line)
            self.js_acks.append(self.pending)
            self.pending = None
        else:
            self.facts.observe(line)
        self.lines.append(line)

    def _publish(self) -> None:
        text = self.text()
        for path in (self.log, self.root / "runtime.log"):
            temporary = path.with_name(path.name + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    stream.write(text)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        self._record()

    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def _progress(self) -> None:
        path = self.root / PROGRESS_NAME
        if not path.exists():
            return
        value = read_live_document(path)
        cycle = value.get("cycle")
        if (
            value.get("schema_version") != 1
            or value.get("mode") != MODE
            or value.get("run_id") != self.run_id
            or type(cycle) is not int
            or not -1 <= cycle <= 9
            or cycle < self.progress_cycle
            or value.get("phase") not in PROGRESS_PHASES
            or value.get("status") not in STATUSES
        ):
            raise RuntimeError("foreign, malformed or rewound physical progress")
        self.progress_cycle, self.progress = cycle, value
        if value["status"] == "failed":
            raise RuntimeError(f"physical recovery progress failed: {value['phase']}")
        if value["phase"] == "recovery-ready" and value.get(
            "recovery_url"
        ) != self.facts.url(cycle):
            raise RuntimeError(
                "physical recovery-ready URL differs from this run/cycle"
            )

    def _poll(self) -> None:
        now = time.monotonic()
        if now >= self.deadline:
            raise TimeoutError("recovery capture absolute timeout exceeded")
        if now >= self.next_guard:
            self.guard()
            self.next_guard = time.monotonic() + PROCESS_SECONDS
        self._progress()
        physical = self.root / PHYSICAL_NAME
        if physical.exists() and read_document(physical).get("status") == "failed":
            raise RuntimeError("physical recovery proof failed")
        if self.stream is not None:
            lines = self.stream.poll()
            observed = 0
            try:
                for line in lines:
                    self._observe(line)
                    observed += 1
            except BaseException as error:
                # Preserve the rejected line and the rest of this captured batch for
                # diagnosis, without admitting them into the validated facts.
                self.lines.extend(lines[observed:])
                failures = [
                    line
                    for line in lines[observed:]
                    if "failed:" in line or ":failed" in line
                ]
                if failures:
                    error.add_note(f"runtime failure in rejected batch: {failures[0]}")
                    self.receipt["runtime_failure"] = failures[0]
                    print(f"recovery-ten-capture:{failures[0]}", flush=True)
                try:
                    self._publish()
                except (OSError, ValueError, RuntimeError) as publication:
                    error.add_note(f"partial capture publication failed: {publication}")
                raise
            if lines:
                self._publish()
        self._ack_deadline()
        if self.facts.bootstrap is None and now >= self.boot_deadline:
            raise TimeoutError("launch-anchored native bootstrap deadline exceeded")
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError("owned capture stream exited before detachment")
        if now >= self.next_heartbeat:
            self._heartbeat()
            self.next_heartbeat = now + 12

    def _heartbeat(self) -> None:
        print(
            f"recovery-ten-capture:{self.platform}:cycle={self.facts.cycle}:"
            f"phase={self.progress.get('phase') if self.progress else 'boot'}:"
            f"actions={len(self.actions)}:native={len(self.native_acks)}:"
            f"js={len(self.js_acks)}",
            flush=True,
        )

    def _android_launch_arguments(self) -> list[str]:
        return [
            "--es",
            "blehub.runtimeMode",
            "rf-recovery-ten",
            "--es",
            "blehub.rfServiceUuid",
            self.service,
            "--es",
            "blehub.rfExpectedLocalName",
            shlex.quote(self.name),
            "--es",
            "blehub.rfRecoveryRunId",
            self.run_id,
        ]

    def _ios_launch_arguments(self) -> list[str]:
        return [
            "--blehub-nitro-rf-recovery-ten-service",
            self.service,
            "--blehub-nitro-rf-expected-local-name",
            self.name,
            "--blehub-nitro-rf-recovery-run-id",
            self.run_id,
        ]

    def _new_stream(self) -> PrefixStream:
        return PrefixStream(
            self.log.with_suffix(".raw.log"),
            self.pid if self.platform == "android" else None,
            prefix=PREFIX,
        )

    def _advance_controls(self) -> None:
        """Composition hook, run outside the reentrant command/stream pump."""

    def _send(self, cycle: int) -> None:
        self._verify_process(f"cycle-{cycle}-before")
        self.guard()
        self._poll()  # Reject unsolicited ACKs already buffered before the request.
        if self.pending is not None or cycle != len(self.actions):
            raise RuntimeError("recovery control schedule drift")
        url = self.facts.url(cycle)
        # Mark pending BEFORE dispatch: native stdout may precede SDK return.
        self.pending, self.sent_at = cycle, time.monotonic()
        self.facts.authorized = cycle
        self.actions.append(cycle)
        action = {
            "cycle": cycle,
            "url": url,
            "requested_at": self.sent_at,
            "status": "requested",
        }
        self.receipt["actions"].append(action)
        self._relay(url, f"cycle-{cycle}")
        action["status"] = "relayed-process-stable"
        self._record()

    def _finished(self) -> bool:
        path = self.root / PHYSICAL_NAME
        if "passed" not in self.facts.seen or not path.exists():
            return False
        physical = read_document(path)
        if (
            physical.get("run_id") != self.run_id
            or physical.get("service_uuid") != self.service
            or physical.get("mode") != MODE
            or physical.get("status") != "passed"
            or physical.get("native_bootstrap") != self.facts.bootstrap
            or physical.get("native_signals")
            != {str(k): v for k, v in self.facts.signals.items()}
        ):
            raise RuntimeError("foreign/incomplete physical recovery proof")
        expected = list(range(10))
        if (
            self.actions != expected
            or self.native_acks != expected
            or self.js_acks != expected
            or self.pending is not None
            or self.progress is None
            or self.progress.get("phase") != "closed"
            or self.progress.get("status") != "passed"
            or self.progress.get("physical_closed") is not True
        ):
            raise RuntimeError(
                "capture completion lacks ten controls/ACKs/physical closure"
            )
        return True

    def run(self) -> tuple[str, tuple[int, ...]]:
        primary: BaseException | None = None
        succeeded = False
        try:
            self._launch()
            while True:
                self._poll()
                if self.facts.bootstrap is not None and not self.bootstrap_verified:
                    self._verify_process("bootstrap")
                    self.bootstrap_verified = True
                if (
                    self.platform == "android"
                    and self.bootstrap_verified
                    and time.monotonic() >= self.next_pid
                ):
                    self._verify_process("periodic")
                if self._finished():
                    self._verify_process("closed")
                    self._poll()
                    break
                if self.bootstrap_verified:
                    self._advance_controls()
                cycle = len(self.actions)
                if (
                    self.bootstrap_verified
                    and self.pending is None
                    and cycle < 10
                    and self.progress is not None
                    and self.progress["phase"] == "recovery-ready"
                    and self.progress["cycle"] == cycle
                    and f"{cycle}:recovery-awaiting" in self.facts.seen
                ):
                    self._send(cycle)
                time.sleep(POLL_SECONDS)
            if self.platform == "ios":
                assert self.process is not None
                self.receipt["console_cleanup"] = stop_local(self.process, console=True)
                self.process = None
                self.receipt["console_action"] = "detached"
                self._poll()  # Drain the final complete prefix after local detach.
                self._verify_process("detached")
            self.guard()
            if not self._finished():
                raise RuntimeError("physical closure changed at final boundary")
            succeeded = True
        except BaseException as error:
            primary = error
            self.identity.update(status="failed", primary_error=str(error))
            self.receipt.update(status="failed", primary_error=str(error))
            latest = self.lines[-1][:1024] if self.lines else "no runtime marker"
            detail = f"capture phase={self.progress}: cycle={self.facts.cycle}; latest={latest}"
            print(f"recovery-ten-capture:failed:{detail}", flush=True)
            error.add_note(detail)
            if isinstance(error, subprocess.SubprocessError):
                self.receipt["failure_command"] = {
                    "command": getattr(error, "cmd", None),
                    "timeout": getattr(error, "timeout", None),
                    "returncode": getattr(error, "returncode", None),
                    "output": str(getattr(error, "output", ""))[:8192],
                }
            raise
        finally:
            cleanup_errors = []
            if self.process is not None:
                try:
                    self.receipt["console_cleanup"] = stop_local(
                        self.process, console=self.platform == "ios"
                    )
                except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                    cleanup_errors.append(error)
            if self.output is not None:
                try:
                    self.output.close()
                except OSError as error:
                    cleanup_errors.append(error)
            if cleanup_errors:
                succeeded = False
                self.identity["status"] = self.receipt["status"] = "failed"
                self.receipt["cleanup_errors"] = [str(e) for e in cleanup_errors]
            if succeeded:
                self.identity["status"] = self.receipt["status"] = "passed"
            try:
                self._record()
            except (OSError, ValueError, RuntimeError) as error:
                cleanup_errors.append(error)
                self.identity["status"] = self.receipt["status"] = "failed"
                self.receipt["publication_error"] = str(error)
                try:
                    self._record()
                except (OSError, ValueError, RuntimeError) as retry_error:
                    error.add_note(
                        f"failed-state publication also failed: {retry_error}"
                    )
            if primary is not None:
                for error in cleanup_errors:
                    primary.add_note(f"capture cleanup/publication failed: {error}")
            elif cleanup_errors:
                raise cleanup_errors[0]
        return self.text(), tuple(self.actions)


def capture(
    platform: str,
    device: str,
    log: Path,
    coordination: Path,
    run_id: str,
    service_uuid: str,
    local_name: str,
    *,
    timeout: float,
    guard: Callable[[], None],
    install_json: Path | None = None,
    owner: Callable[..., _Capture] | None = None,
) -> tuple[str, tuple[int, ...]]:
    """Capture ten same-process controls; timeout is absolute (normally 900s).

    The parent has installed the frozen host and live-attested fixture, and on
    Android has force-stopped, cleared logcat and granted permissions. ``guard``
    revalidates that current fixture/receipt, periodically and at every boundary.
    Only complete validated recovery-prefix lines are published to runtime.log.
    SDK queries/relays have a 1.5s outer budget despite their required --timeout 5;
    pumping continues during commands, and native then JS ACKs share the original
    request's hard 2s deadline. Returned actions are exactly (0, ..., 9).
    """
    if platform not in {"android", "ios"} or not device.strip():
        raise ValueError("capture requires android/ios and an explicit device")
    for value in (run_id, service_uuid):
        if str(uuid.UUID(value)) != value:
            raise ValueError("capture requires canonical run/service UUIDs")
    if not local_name.strip() or len(local_name) > 512:
        raise ValueError("capture requires a bounded local name")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("capture timeout must be finite and positive")
    if not log.parent.is_dir() or not coordination.is_dir():
        raise ValueError("capture requires parent-owned log/coordination directories")
    for path in (
        log,
        log.with_suffix(".raw.log"),
        log.with_suffix(".identity.json"),
        log.with_suffix(".capture.json"),
        coordination / "runtime.log",
    ):
        if path.is_symlink() or path.exists() and path.stat().st_size:
            raise ValueError(f"capture refuses stale evidence: {path}")
    return (owner or _Capture)(
        platform,
        device,
        log,
        coordination,
        run_id,
        service_uuid,
        local_name,
        timeout,
        guard,
        install_json,
    ).run()
