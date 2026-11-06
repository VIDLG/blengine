"""Shared single-use REJECT control transport, not a scenario self.protocol.

Named adapters supply provider replay/relay and ledger verification. This owner
retains one raw stream, frozen identity, SDK receipts, ordered native/JS ACKs
and local console cleanup; it never defines BLE steps or physical requests.
"""

from __future__ import annotations

import json
import math
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tools.react_native.capture_io import PrefixStream, stop_local
from tools.react_native.capture_transport import (
    COMMAND_SECONDS,
    POLL_SECONDS,
    PROCESS_SECONDS,
    CaptureTransport,
)
from tools.react_native.host_contract import ANDROID_PACKAGE, BUNDLE_ID
from tools.react_native.ios_sdk import (
    devicectl_post_notification,
    official_result,
)

ACK_SECONDS = 2.0
MAX_TIMEOUT = 300.0


def _same(actual: Any, expected: Any, label: str, require: Callable) -> None:
    require(type(actual) is type(expected), label)
    if isinstance(expected, dict):
        require(actual.keys() == expected.keys(), label)
        for key, value in expected.items():
            _same(actual[key], value, f"{label}.{key}", require)
    else:
        require(actual == expected, label)


class RejectCapture(CaptureTransport):
    """Direct transport specialization; never inherits recovery-cycle state.

    Both native and JS ACKs must follow consumption and precede the next provider
    request. _poll pumps/records only: relay advancement occurs outside reentrant
    SDK commands. ACKs arriving before SDK return remain visible and valid, but a
    failed SDK command cannot authorize the next provider action.
    """

    protocol: ModuleType
    scenario: str
    android_control_property: str

    def _new_relay(self) -> Any:
        raise NotImplementedError

    def _verify_ledger(self) -> None:
        raise NotImplementedError

    def _before_control(self, phase: str) -> None:
        """Optional named-adapter precondition; provider owns protocol facts."""

    def __init__(
        self,
        platform: str,
        device: str,
        log: Path,
        coordination: Path,
        run_id: str,
        service_uuid: str,
        expected_local_name: str,
        timeout: float,
        guard: Callable[[], None],
        install_json: Path | None,
        *,
        fixture_identity: tuple[int, int],
    ) -> None:
        self.plan = fixture_session.load_session(
            coordination / "fixture-session/fixture-request.json"
        )
        self.control = self.protocol.validate_control(coordination, self.plan)
        profile = fixture_session.load_session_profile(
            self.plan.contract, self.plan.profile_path
        )
        self.protocol.require(
            (self.plan.session_id, self.plan.service_uuid, profile.local_name)
            == (run_id, service_uuid, expected_local_name),
            "capture frozen fixture tuple",
        )
        self.protocol.require(
            len(fixture_identity) == 2
            and all(type(v) is int and v > 0 for v in fixture_identity),
            "capture fixture identity",
        )
        self.fixture_identity = fixture_identity
        self.parent_guard = guard
        self.request_bytes = self.plan.request_path.read_bytes()
        self.control_bytes = (coordination / self.protocol.CONTROL_NAME).read_bytes()
        self.initial_receipt = fixture_session.verify_receipt(
            self.plan, fixture_identity=fixture_identity
        )
        super().__init__(
            platform,
            device,
            log,
            coordination,
            run_id,
            service_uuid,
            expected_local_name,
            timeout,
            self._guard,
            install_json,
        )
        self.relay = self._new_relay()
        self.bootstrap: dict[str, Any] | None = None
        self.bootstrap_verified = False
        self.native_signals: list[dict[str, Any]] = []
        self.native_acknowledged: list[str] = []
        self.js_acknowledged: list[str] = []
        self.actions: list[dict[str, Any]] = []
        self.pending: str | None = None
        self.sent_at: float | None = None
        self.in_relay = False
        self.facts: dict[str, Any] | None = None
        self.next_heartbeat = self.started
        self.identity.update(scenario=self.scenario, control=asdict(self.control))
        self.receipt.update(
            scenario=self.scenario,
            control=asdict(self.control),
            cleanup_errors=[],
            native_signal_scope="native identity+phase; source/URL not exported by host",
        )
        self.receipt["budgets_seconds"]["ack"] = ACK_SECONDS

    def _guard(self) -> None:
        self.parent_guard()
        self.protocol.require(
            self.plan.request_path.read_bytes() == self.request_bytes
            and (self.root / self.protocol.CONTROL_NAME).read_bytes()
            == self.control_bytes,
            "capture inputs drift",
        )
        _same(
            fixture_session.verify_receipt(
                self.plan, fixture_identity=self.fixture_identity
            ),
            self.initial_receipt,
            "capture live receipt drift",
            self.protocol.require,
        )

    def text(self) -> str:
        return "".join(line + "\n" for line in self.lines)

    def _android_launch_arguments(self) -> list[str]:
        return [
            "--es",
            "blehub.runtimeMode",
            self.protocol.RUNTIME_MODE,
            "--es",
            "blehub.rfServiceUuid",
            self.service,
            "--es",
            "blehub.rfExpectedLocalName",
            shlex.quote(self.name),
            "--es",
            self.android_control_property,
            shlex.quote(json.dumps(asdict(self.control))),
        ]

    def _ios_launch_arguments(self) -> list[str]:
        return [
            f"--blehub-nitro-rf-{self.scenario}-service",
            self.service,
            "--blehub-nitro-rf-expected-local-name",
            self.name,
            f"--blehub-nitro-rf-{self.scenario}-control",
            json.dumps(asdict(self.control)),
        ]

    def _new_stream(self) -> PrefixStream:
        return PrefixStream(
            self.log.with_suffix(".raw.log"),
            self.pid if self.platform == "android" else None,
            prefix=self.protocol.PREFIX,
            raw=True,
        )

    def _record(self) -> None:
        self.identity.update(
            bootstrap=self.bootstrap,
            pid=self.pid,
            executable=self.executable,
            device_identifier=self.device_identifier,
            fixture_identity={
                "pid": self.fixture_identity[0],
                "pgid": self.fixture_identity[1],
            },
            native_signals=list(self.native_signals),
            native_acknowledged=list(self.native_acknowledged),
            js_acknowledged=list(self.js_acknowledged),
            launch_started_at=self.receipt.get("launch_started_at"),
            bootstrap_deadline=self.boot_deadline,
        )
        self.receipt.update(
            actions=self.actions,
            native_bootstrap=self.bootstrap,
            native_signals=list(self.native_signals),
            native_acknowledged=list(self.native_acknowledged),
            js_acknowledged=list(self.js_acknowledged),
            pending=self.pending,
            normalized_lines=len(self.lines),
        )
        write_document(self.log.with_suffix(".identity.json"), self.identity)
        write_document(self.log.with_suffix(".capture.json"), self.receipt)

    def _publish(self) -> None:
        temporary = self.log.with_name(self.log.name + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(self.text())
            temporary.replace(self.log)
        finally:
            temporary.unlink(missing_ok=True)
        self._record()

    def _command(self, command: list[str], *, budget: float = COMMAND_SECONDS) -> str:
        delivery = self.in_relay and (
            "android.intent.action.VIEW" in command
            or "notification" in command
            and "post" in command
        )
        if delivery:
            self.actions[-1]["sdk_command"] = command
        output = super()._command(command, budget=budget)
        if delivery:
            path = self.log.with_suffix(
                f".control-{len(self.actions) - 1}-sdk-output.log"
            )
            with path.open("x", encoding="utf-8") as stream:
                stream.write(output)
            self.actions[-1].update(sdk_exit=0, sdk_output=path.name)
        return output

    def _relay(
        self, url: str, boundary: str, *, notification: str | None = None
    ) -> None:
        if self.platform == "android":
            super()._relay(url, boundary)
            return
        self.protocol.require(
            self.pending is not None
            and notification == self.control.notification(self.pending)
            and url == self.control.uri(self.pending),
            "notification route",
        )
        path = self.log.with_suffix(f".{boundary}-notification.json")
        self.protocol.require(
            not path.exists() and not path.is_symlink(), "stale notification receipt"
        )
        command = devicectl_post_notification(self.device, notification, path)
        self.actions[-1]["sdk_receipt"] = path.name
        output = self._command(command)
        self.protocol.require(
            "Error:" not in output and "Exception" not in output,
            "notification SDK rejection",
        )
        result = official_result(
            path, "devicectl.device.notification.post", self.device
        )
        arguments = read_document(path)["info"]["arguments"]
        self.protocol.require(
            result["deviceIdentifier"] == self.device_identifier
            and arguments.count("--name") == 1
            and arguments[arguments.index("--name") + 1 :]
            and arguments[arguments.index("--name") + 1] == notification,
            "official notification device/name receipt",
        )
        self._verify_process(f"{boundary}-after")
        self._poll()

    def _request_deadline(self) -> float:
        return (
            min(self.deadline, self.sent_at + ACK_SECONDS)
            if self.sent_at is not None and (self.pending is not None or self.in_relay)
            else self.deadline
        )

    def _identity_budget(self, boundary: str) -> float:
        budget = min(COMMAND_SECONDS, self._request_deadline() - time.monotonic())
        if budget <= 0:
            raise TimeoutError(
                f"{self.scenario} identity/ACK budget exhausted: {boundary}"
            )
        return budget

    def _ack_deadline(self) -> None:
        if time.monotonic() >= self._request_deadline():
            raise TimeoutError(
                f"{self.scenario} original native/JS ACK deadline exceeded"
            )

    def _observe(self, body: str) -> None:
        if body.startswith("rf-host:bootstrap:"):
            value = fixture_session._json(
                body.removeprefix("rf-host:bootstrap:").encode()
            )
            self.protocol.require(
                self.bootstrap is None and time.monotonic() < self.boot_deadline,
                "duplicate/late native bootstrap",
            )
            pid, boot = value.get("pid"), value.get("boot_id")
            self.protocol.require(
                type(pid) is int and pid > 0 and (self.pid is None or pid == self.pid),
                "bootstrap PID",
            )
            self.protocol.require(
                isinstance(boot, str) and str(uuid.UUID(boot)) == boot,
                "bootstrap boot UUID",
            )
            expected = {
                **asdict(self.control),
                "pid": pid,
                "boot_id": boot,
                "bundle_id": ANDROID_PACKAGE
                if self.platform == "android"
                else BUNDLE_ID,
                "executable": self.executable,
            }
            _same(value, expected, "native bootstrap", self.protocol.require)
            self.pid, self.bootstrap = pid, value
            self.identity["status"] = "pinned"
        elif body.startswith("rf-host:signal:"):
            self._ack_deadline()
            self.protocol.require(
                self.pending is not None
                and self.bootstrap is not None
                and len(self.native_acknowledged) == len(self.js_acknowledged),
                "early/duplicate native ACK",
            )
            value = fixture_session._json(body.removeprefix("rf-host:signal:").encode())
            _same(
                value,
                {**self.bootstrap, "phase": self.pending},
                "native signal",
                self.protocol.require,
            )
            self.native_signals.append(value)
            self.native_acknowledged.append(self.pending)
            self.actions[-1]["native_ack_at"] = time.monotonic()
        elif body.startswith("rf-control:observed:"):
            self._ack_deadline()
            self.protocol.require(
                self.pending is not None
                and len(self.native_acknowledged) == len(self.js_acknowledged) + 1
                and body == self.control.marker("observed", self.pending),
                "early/foreign/duplicate JS ACK or JS before native",
            )
            self.js_acknowledged.append(self.pending)
            self.actions[-1]["js_ack_at"] = time.monotonic()
            self.pending = None
        elif body.startswith("rf-host:"):
            raise ValueError(f"{self.scenario} unknown/failed native host marker")
        elif body.startswith(("rf-control:", f"rf-{self.scenario}:")):
            self.protocol.require(
                self.bootstrap is not None, "native bootstrap missing before JS/control"
            )
            self.protocol.require(
                not body.startswith(
                    ("rf-control:host-failed:", f"rf-{self.scenario}:failed:")
                ),
                "native/App failure",
            )

    def _drain(self) -> None:
        if self.stream is None:
            return
        incoming = self.stream.poll()
        selected = []
        for raw in incoming:
            body = raw.split(self.protocol.PREFIX, 1)[1].strip()
            if body.startswith((f"rf-{self.scenario}:", "rf-control:", "rf-host:")):
                selected.append(self.protocol.PREFIX + body)
        self.lines.extend(
            selected
        )  # Rejected records remain in the diagnostic projection.
        try:
            for line in selected:
                self._observe(line.removeprefix(self.protocol.PREFIX))
            if selected:
                self.facts = self.protocol.runtime_facts(
                    self.text(), run_id=self.run_id, platform=self.platform
                )
        except BaseException as error:
            try:
                self._publish()
            except (OSError, ValueError, RuntimeError) as publication:
                error.add_note(f"rejected capture publication: {publication}")
            raise
        else:
            if selected:
                self._publish()

    def _physical(self) -> dict[str, Any] | None:
        path = self.root / self.protocol.PHYSICAL_NAME
        if not path.exists():
            return None
        value = fixture_session.read_live_document(path)
        self.protocol.require(
            value.get("control") == asdict(self.control)
            and value.get("session_id") == self.run_id
            and value.get("service_uuid") == self.service
            and value.get("fixture_pid") == self.fixture_identity[0]
            and value.get("fixture_pgid") == self.fixture_identity[1],
            "foreign physical writer",
        )
        self.protocol.require(
            value.get("status") in ("running", "passed")
            and not value.get("primary_error")
            and not value.get("cleanup_errors"),
            f"physical writer failed: {value.get('primary_error')}; cleanup={value.get('cleanup_errors')}",
        )
        return value

    def _poll(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError(f"{self.scenario} capture absolute timeout")
        if time.monotonic() >= self.next_guard:
            self._guard()
            self.next_guard = time.monotonic() + PROCESS_SECONDS
        self._drain()
        self._physical()
        self._ack_deadline()
        if self.bootstrap is None and time.monotonic() >= self.boot_deadline:
            raise TimeoutError(f"{self.scenario} launch-anchored bootstrap deadline")
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(f"{self.scenario} console/capture exited before detach")
        if time.monotonic() >= self.next_heartbeat:
            print(
                f"{self.scenario}-capture:{self.platform}:controls={len(self.js_acknowledged)}/{len(self.protocol.PHASES)}:pending={self.pending}",
                flush=True,
            )
            self.next_heartbeat = time.monotonic() + 12

    def _advance(self) -> None:
        if self.pending is not None:
            return
        self.relay.observe(self.text())
        phase = self.relay.pending()
        if phase is None:
            return
        self._verify_process(f"control-{len(self.actions)}-before")
        self._poll()
        self.protocol.require(
            self.bootstrap_verified, "unverified bootstrap before control"
        )
        self.facts = self.protocol.runtime_facts(
            self.text(), run_id=self.run_id, platform=self.platform
        )
        self._before_control(phase)
        self.relay.forwarded(phase)
        self.pending, self.sent_at, self.in_relay = phase, time.monotonic(), True
        action = {
            "control": asdict(self.control),
            "phase": phase,
            "requested_at": self.sent_at,
            "url": self.control.uri(phase),
            "notification": self.control.notification(phase)
            if self.platform == "ios"
            else None,
            "delivery": "darwin-notification"
            if self.platform == "ios"
            else "android-intent",
            "status": "requested",
        }
        self.actions.append(action)
        self._record()
        primary: BaseException | None = None
        try:
            self._relay(
                action["url"],
                f"control-{len(self.actions) - 1}",
                notification=action["notification"],
            )
            action["status"] = "relayed-process-stable"
        except BaseException as error:
            primary = error
            raise
        finally:
            self.in_relay = False
            try:
                self._record()
            except (OSError, ValueError, RuntimeError) as publication:
                if primary is None:
                    raise
                primary.add_note(f"SDK failure publication: {publication}")

    def _finished(self) -> bool:
        physical = self._physical()
        if (
            not self.facts
            or not self.facts["passed"]
            or physical is None
            or physical["status"] != "passed"
        ):
            return False
        self.protocol.require(
            self.pending is None
            and self.relay.sent == list(self.protocol.PHASES)
            and self.native_acknowledged
            == self.js_acknowledged
            == list(self.protocol.PHASES)
            and all(a["status"] == "relayed-process-stable" for a in self.actions),
            "capture completion lacks all SDK/native/JS controls",
        )
        self._verify_ledger()
        return True

    def run(self) -> tuple[str, tuple[int, ...]]:
        primary: BaseException | None = None
        succeeded = False
        try:
            self._launch()
            while True:
                self._poll()
                if self.bootstrap is not None and not self.bootstrap_verified:
                    self._verify_process("bootstrap")
                    self.bootstrap_verified = True
                if (
                    self.bootstrap_verified
                    and self.platform == "android"
                    and time.monotonic() >= self.next_pid
                ):
                    self._verify_process("periodic")
                if self.bootstrap_verified:
                    self._advance()
                if self._finished():
                    self._verify_process("closed")
                    self._poll()
                    break
                time.sleep(POLL_SECONDS)
            self._guard()
            succeeded = True
        except BaseException as error:
            primary = error
            self.identity.update(
                status="failed", primary_error=f"{type(error).__name__}: {error}"
            )
            self.receipt.update(
                status="failed", primary_error=f"{type(error).__name__}: {error}"
            )
            if isinstance(error, subprocess.SubprocessError):
                self.receipt["failure_command"] = {
                    "command": getattr(error, "cmd", None),
                    "timeout": getattr(error, "timeout", None),
                    "returncode": getattr(error, "returncode", None),
                    "output": str(getattr(error, "output", ""))[:8192],
                }
            raise
        finally:
            errors: list[BaseException] = []
            try:
                if self.process is not None:
                    self.receipt["console_cleanup"] = stop_local(
                        self.process, console=self.platform == "ios"
                    )
                    self.process = None
                self._drain()
                if succeeded:
                    self._verify_process("detached")
                    self.protocol.require(
                        self._finished(), "closure drift after console detach"
                    )
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                errors.append(error)
            finally:
                if self.output is not None:
                    try:
                        self.output.close()
                    except OSError as error:
                        errors.append(error)
            succeeded = succeeded and not errors
            self.identity["status"] = self.receipt["status"] = (
                "passed" if succeeded else "failed"
            )
            self.receipt["cleanup_errors"] = [str(e) for e in errors]
            try:
                self._record()
            except (OSError, ValueError, RuntimeError) as error:
                errors.append(error)
                self.identity["status"] = self.receipt["status"] = "failed"
                self.receipt["publication_error"] = str(error)
                self.receipt["cleanup_errors"] = [str(e) for e in errors]
                try:
                    self._record()
                except (OSError, ValueError, RuntimeError) as publication:
                    error.add_note(f"failed capture publication: {publication}")
            if primary is not None:
                for error in errors:
                    primary.add_note(f"capture cleanup/publication: {error}")
            elif errors:
                raise errors[0]
        return self.text(), ()


def capture_reject(
    capture_type: type[RejectCapture],
    platform: str,
    device: str,
    log: Path,
    coordination: Path,
    run_id: str,
    service_uuid: str,
    expected_local_name: str,
    *,
    timeout: float,
    guard: Callable[[], None],
    install_json: Path | None = None,
    fixture_identity: tuple[int, int],
) -> tuple[str, tuple[int, ...]]:
    """Return normalized three-prefix text and (), never RecoveryTen cycle IDs.

    Writes log, log.raw.log, log.identity.json and log.capture.json (Path suffix
    replacement). The relay alone publishes coordination/runtime.log once both
    ACK sources and successful SDK completion permit provider advancement.
    """
    protocol = capture_type.protocol
    protocol.require(
        platform in ("android", "ios")
        and bool(device.strip())
        and device == device.strip(),
        "capture platform/device",
    )
    for value in (run_id, service_uuid):
        protocol.require(str(uuid.UUID(value)) == value, "capture canonical UUID")
    protocol.require(
        bool(expected_local_name.strip()) and len(expected_local_name) <= 128,
        "capture local name",
    )
    protocol.require(
        type(timeout) in (int, float)
        and math.isfinite(timeout)
        and 0 < timeout <= MAX_TIMEOUT,
        "capture timeout in (0, 300]",
    )
    protocol.require(
        log.parent.is_dir() and coordination.is_dir(),
        "parent-owned capture directories",
    )
    for path in (
        log,
        log.with_suffix(".raw.log"),
        log.with_suffix(".identity.json"),
        log.with_suffix(".capture.json"),
        coordination / "runtime.log",
    ):
        protocol.require(
            not path.is_symlink() and not (path.exists() and path.stat().st_size),
            f"stale capture evidence: {path}",
        )
    return capture_type(
        platform,
        device,
        log,
        coordination,
        run_id,
        service_uuid,
        expected_local_name,
        timeout,
        guard,
        install_json,
        fixture_identity=fixture_identity,
    ).run()
