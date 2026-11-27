"""Explicit prebuilt production Apple HIL smoke owner (one IO or notification session)."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from tests.peripherals.nrf52840.cancellation_control import require_multifunction
from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.fault_control import FaultController
from tests.peripherals.nrf52840.fixture_session import (
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tools import host_command
from tools.apple.app_artifact import fingerprint
from tools.apple.rf_fixture import RfFixtureConfig
from tools.apple.rf_fixture import load_profile as apple_profile
from tools.react_native.ios_device import (
    canonical_physical_udid,
    require_ready_unlocked_device,
)
from tools.react_native.ios_sdk import (
    devicectl_copy_from,
    devicectl_install_app,
    devicectl_processes,
    devicectl_terminate_process,
    official_result,
)

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work/apple-native-hil"
MAX_BYTES = 1024 * 1024


def read_text(path: Path) -> str:
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise RuntimeError(f"linked/oversized native HIL evidence: {path}")
    return path.read_text(encoding="utf-8")


def executable_path(value: str) -> str:
    """CoreDevice uses file URLs; Foundation publishes an absolute executable path."""
    parsed = urlparse(value)
    if parsed.scheme == "file" and not parsed.netloc:
        value = unquote(parsed.path)
        return "/private" + value if value.startswith("/var/") else value
    if parsed.scheme or not value.startswith("/"):
        raise RuntimeError("invalid native executable identity")
    return "/private" + value if value.startswith("/var/") else value


class NativeSmoke:
    """Own only this native App launch; no build, signing, radio or provider CLI."""

    def __init__(self, platform: str, device: str, app: Path, attempt: Path) -> None:
        self.platform, self.device, self.app, self.attempt = (
            platform,
            device,
            app,
            attempt,
        )
        plist = app / ("Contents/Info.plist" if platform == "macos" else "Info.plist")
        info = plistlib.loads(plist.read_bytes())
        if (
            type(info.get("BleHubNativeHilProtocolVersion")) is not int
            or info["BleHubNativeHilProtocolVersion"] != 2
        ):
            raise ValueError(
                "prebuilt App lacks native HIL ownership protocol v2 (combined scan filter); rebuild explicitly"
            )
        self.bundle = info["CFBundleIdentifier"]
        self.name = info["CFBundleExecutable"]
        expected = (
            "BleHubAppleMacOSProbe" if platform == "macos" else "BleHubAppleIOSProbe"
        )
        if self.name != expected or not isinstance(self.bundle, str) or not self.bundle:
            raise ValueError(
                "prebuilt artifact is not the selected production native HIL App"
            )
        self.executable = (
            app / ("Contents/MacOS" if platform == "macos" else "") / self.name
        )
        if not self.executable.is_file():
            raise ValueError("prebuilt HIL executable is missing")
        self.pid: int | None = None
        self.boot: dict[str, Any] | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.output: Any = None
        self.sdk_count = 0
        self.installed_device: str | None = None
        self.launch_attempted = False
        self.termination_attempted = False
        self.termination_confirmed = False
        self.require_device_owner: Callable[[], None] = lambda: None
        self.name_prefix: str | None = None
        self.notification_stimuli: list[dict[str, Any]] = []
        self.notification_release_attempted = False
        self.notification_marker_released = False
        self.run_id = attempt.name
        self.deadline = time.monotonic() + 300
        self.digest = fingerprint(app)

    def budget(self, maximum: float = 15) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native HIL whole-session deadline exceeded")
        return min(maximum, remaining)

    def sdk(
        self, command: list[str], *, optional: bool = False
    ) -> dict[str, Any] | None:
        self.require_device_owner()
        self.sdk_count += 1
        receipt = self.attempt / f"sdk-{self.sdk_count}.json"
        command = [str(shutil.which("xcrun") or "xcrun"), *command[1:]]
        index = command.index("--json-output")
        command[index + 1] = str(receipt)
        with (self.attempt / f"sdk-{self.sdk_count}.log").open("wb") as log:
            result = subprocess.run(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=self.budget(30),
            )
        self.budget()
        if result.returncode:
            if optional:
                return None
            raise RuntimeError(
                f"Apple SDK failed ({result.returncode}); see {log.name}"
            )
        read_text(receipt)
        kind = ".".join(command[1:5])
        document = official_result(receipt, kind, self.device)
        observed = document["deviceIdentifier"]
        if self.installed_device is not None and observed != self.installed_device:
            raise RuntimeError("CoreDevice identity changed")
        self.installed_device = observed
        return document

    def processes(self) -> list[dict[str, Any]]:
        result = self.sdk(
            devicectl_processes(self.device, self.attempt / "unused.json")
        )
        assert result is not None
        rows = result.get("runningProcesses")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("missing/malformed CoreDevice process list")
        return rows

    def refuse_existing(self) -> None:
        if any(
            executable_path(row.get("executable", "")).endswith("/" + self.name)
            for row in self.processes()
        ):
            raise RuntimeError("native HIL App already running; refusing takeover")

    def install(self, guard: Callable[[], None]) -> None:
        if fingerprint(self.app) != self.digest:
            raise RuntimeError("snapshotted App changed")
        if self.platform == "ios":
            self.refuse_existing()
            guard()
            self.sdk(
                devicectl_install_app(
                    self.device, self.app, self.attempt / "unused.json"
                )
            )

    def launch(
        self,
        config: RfFixtureConfig,
        scenario: str,
        coordination: Path,
        guard: Callable[[], None],
        *,
        name_prefix: str,
    ) -> None:
        if fingerprint(self.app) != self.digest:
            raise RuntimeError("snapshotted App changed before launch")
        if not name_prefix:
            raise ValueError(
                "native HIL requires the canonical profile local_name prefix"
            )
        self.name_prefix = name_prefix
        arguments = [
            "--native-hil",
            "--run-id",
            self.run_id,
            "--scan-name-prefix",
            name_prefix,
            *config.app_arguments(),
        ]
        if self.platform == "ios":
            self.refuse_existing()
            arguments += [
                "--rf-service-uuid",
                config.serviceUuid,
                "--rf-scenario",
                scenario,
                "--rf-external-provider",
                "nrf52840",
            ]
            guard()
            self.launch_attempted = True
            result = self.sdk(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "process",
                    "launch",
                    "--device",
                    self.device,
                    "--activate",
                    "--json-output",
                    "unused",
                    self.bundle,
                    *arguments,
                ]
            )
            assert result is not None
            process = result.get("process", {})
            self.pid = process.get("processIdentifier")
            self.launched_executable = executable_path(process.get("executable", ""))
            if (
                type(self.pid) is not int
                or self.pid <= 0
                or not self.launched_executable.endswith("/" + self.name)
            ):
                raise RuntimeError("launch did not prove native PID/executable")
        else:
            arguments += [
                "--scan-service-uuid",
                config.serviceUuid,
                "--connect-after-scan",
                "--discover-gatt-after-connect",
                "--fixture-coordination-path",
                str(coordination),
                "--result-path",
                str(coordination / "result.txt"),
                "--run-gatt-io" if scenario == "io" else "--run-notifications",
            ]
            self.output = (self.attempt / "native.log").open("wb")
            guard()
            self.launch_attempted = True
            self.process = subprocess.Popen(
                [str(self.executable), *arguments],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.STDOUT,
            )
            self.pid = self.process.pid
            self.launched_executable = str(self.executable)

    def pull(self, name: str, coordination: Path) -> str | None:
        if self.platform == "macos":
            path = coordination / name
            return read_text(path) if path.exists() else None
        destination = self.attempt / ("pulled-" + name)
        destination.unlink(missing_ok=True)
        result = self.sdk(
            devicectl_copy_from(
                self.device,
                f"Documents/blehub-rf-{self.run_id}/{name}",
                self.bundle,
                destination,
                self.attempt / "unused.json",
                timeout=5,
            ),
            optional=True,
        )
        return (
            read_text(destination)
            if result is not None and destination.is_file()
            else None
        )

    def identity(self, coordination: Path) -> bool:
        text = self.pull("native-identity.json", coordination)
        if text is None:
            return False
        value = json.loads(text)
        if (
            set(value) != {"run_id", "boot_id", "pid", "bundle_id", "executable"}
            or value["run_id"] != self.run_id
            or type(value["pid"]) is not int
            or value["pid"] != self.pid
            or value["bundle_id"] != self.bundle
            or executable_path(value["executable"]) != self.launched_executable
            or str(uuid.UUID(value["boot_id"])) != value["boot_id"]
        ):
            raise RuntimeError("foreign native PID/boot/App identity")
        if self.boot is not None and self.boot != value:
            raise RuntimeError("native boot identity changed")
        self.boot = value
        return True

    def copy_to(self, path: Path) -> None:
        if self.platform == "ios":
            self.sdk(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "copy",
                    "to",
                    "--device",
                    self.device,
                    "--source",
                    str(path),
                    "--destination",
                    f"Documents/blehub-rf-{self.run_id}/{path.name}",
                    "--domain-type",
                    "appDataContainer",
                    "--domain-identifier",
                    self.bundle,
                    "--json-output",
                    "unused",
                ]
            )

    def trigger_notification(
        self,
        fixture: DirectFixture,
        handle: int,
        baseline: bytes,
        coordination: Path,
        guard: Callable[[], None],
        *,
        post_disable: bool = False,
    ) -> None:
        expected_count = 1 if post_disable else 0
        if (
            self.notification_release_attempted
            or len(self.notification_stimuli) != expected_count
        ):
            raise RuntimeError("duplicate/out-of-order native notification stimulus")
        if not 1 <= len(baseline) <= 512:
            raise ValueError("native notification baseline must contain 1–512 bytes")
        enabled = self.pull("notification-enabled", coordination)
        disabled = self.pull("notification-disabled", coordination)
        if enabled != "ready\n" or (
            disabled != "ready\n" if post_disable else disabled is not None
        ):
            raise RuntimeError(
                "native notification stimulus requires enabled then disabled ordering"
            )
        if (
            post_disable
            and not self.notification_stimuli[0]["btp_set_value_acknowledged"]
        ):
            raise RuntimeError("post-disable stimulus requires acknowledged baseline")
        path = coordination / "post-disable-triggered"
        if path.exists() or self.pull(path.name, coordination) is not None:
            raise RuntimeError("native notification marker released before stimulus")
        value = bytes([baseline[0] ^ 1]) + baseline[1:] if post_disable else baseline
        entry = {
            "phase": "post-disable" if post_disable else "enabled-baseline",
            "role": "updates",
            "handle": handle,
            "baseline_value_hex": baseline.hex(),
            "value_hex": value.hex(),
            "btp_set_value_acknowledged": False,
        }
        guard()
        self.notification_stimuli.append(entry)  # Never retry an attempted SET_VALUE.
        fixture._gatt.set_value(handle, value)
        entry["btp_set_value_acknowledged"] = True

    def release_disable(self, coordination: Path, guard: Callable[[], None]) -> None:
        if (
            self.notification_release_attempted
            or len(self.notification_stimuli) != 2
            or not all(
                entry["btp_set_value_acknowledged"]
                for entry in self.notification_stimuli
            )
        ):
            raise RuntimeError("early/duplicate native notification marker release")
        if self.pull("notification-disabled", coordination) != "ready\n":
            raise RuntimeError("native notification marker release requires disabled")
        path = coordination / "post-disable-triggered"
        if path.exists() or self.pull(path.name, coordination) is not None:
            raise RuntimeError("early/duplicate native notification marker release")
        guard()
        self.notification_release_attempted = True
        path.write_text("ready\n", encoding="ascii")
        self.copy_to(path)
        self.notification_marker_released = True

    def inspect_live_boot(self, coordination: Path) -> None:
        assert self.boot is not None
        challenge = str(uuid.uuid4())
        path = coordination / "identity-request.json"
        path.write_text(
            json.dumps({"boot_id": self.boot["boot_id"], "challenge": challenge}),
            encoding="utf-8",
        )
        self.copy_to(path)
        name = f"dev.blehub.native-hil.{self.run_id}.{self.boot['boot_id']}.inspect"
        self.sdk(
            [
                "xcrun",
                "devicectl",
                "device",
                "notification",
                "post",
                "--device",
                self.device,
                "--name",
                name,
                "--timeout",
                "5",
                "--json-output",
                "unused",
            ]
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            text = self.pull("native-inspection.json", coordination)
            if text is not None and json.loads(text) == {
                "identity": self.boot,
                "challenge": challenge,
            }:
                return
            time.sleep(min(0.1, self.budget()))
        raise RuntimeError(
            "no fresh native boot challenge response; refusing PID cleanup"
        )

    def cleanup(
        self, coordination: Path, *, passed: bool, close_on_success: bool = False
    ) -> None:
        self.deadline = time.monotonic() + 20
        try:
            if self.pid is None:
                if self.launch_attempted:
                    raise RuntimeError(
                        "launch outcome/PID unproved; no process termination attempted"
                    )
                return
            if self.platform == "macos" and self.process is not None:
                code = self.process.poll()
                if code is not None:
                    if passed and code != 0:
                        raise RuntimeError(
                            f"native macOS probe exited {code} after result"
                        )
                    return  # Popen retains this child's exit status, never targets a reused PID.
                if passed:
                    try:
                        code = self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        code = None
                    if code is not None:
                        if code != 0:
                            raise RuntimeError(
                                f"native macOS probe exited {code} after result"
                            )
                        return
            if not self.identity(coordination):
                raise RuntimeError("unproved native boot; refusing process cleanup")
            if self.platform == "ios":
                self.inspect_live_boot(coordination)
                rows = [
                    row
                    for row in self.processes()
                    if row.get("processIdentifier") == self.pid
                ]
                if not rows:
                    return
                if (
                    len(rows) != 1
                    or executable_path(rows[0].get("executable", ""))
                    != self.launched_executable
                ):
                    raise RuntimeError("foreign/reused native PID; refusing cleanup")
                if passed and not close_on_success:
                    return  # Preserve the successful iPhone result page by default.
                self.termination_attempted = True
                self.sdk(
                    devicectl_terminate_process(
                        self.device, self.pid, self.attempt / "unused.json"
                    )
                )
                if any(
                    row.get("processIdentifier") == self.pid for row in self.processes()
                ):
                    raise RuntimeError("attempt-owned iOS PID did not terminate")
                self.termination_confirmed = True
            elif self.process is not None:
                self.termination_attempted = True
                host_command.stop(self.process, grace=3)
                self.termination_confirmed = True
                if passed:
                    raise RuntimeError(
                        "native macOS probe did not exit after result; owned child stopped"
                    )
        finally:
            if self.output is not None:
                self.output.close()


def validate_result(
    text: str, native: NativeSmoke, config: RfFixtureConfig, scenario: str
) -> None:
    lines = text.splitlines()
    identity = [
        line.partition("=")[2]
        for line in lines
        if line.startswith("apple_native_hil_identity=")
    ]
    if len(identity) != 1 or json.loads(identity[0]) != native.boot:
        raise RuntimeError("result does not match the pinned native boot")
    prefix = f"apple_{native.platform}_corebluetooth_"
    required = [
        prefix + name + "=passed"
        for name in (
            "initialize",
            "scan",
            "connected",
            "gatt",
            "logical_disconnect",
            "post_disconnect_silence",
        )
    ]
    required += config.evidence() + [
        "apple_native_hil_close=passed",
        prefix + "scan_filter=service+name",
        prefix + "scan_name_prefix=" + str(native.name_prefix),
    ]
    if scenario == "io":
        required += [
            prefix + name + "=passed"
            for name in (
                "write_with_response_readback",
                "write_without_response_readback",
                "gatt_io_event_order",
                "gatt_io",
            )
        ]
    else:
        required += [
            prefix + name + "=passed"
            for name in (
                "notification_enable_disable_order",
                "notification_post_disable_silence",
                "notification",
            )
        ]
        required += [prefix + "notification_value=" + config.notificationValueHex]
    if native.platform == "ios":
        required += [
            f"apple_ios_probe_run_id={native.run_id}",
            f"apple_ios_rf_{scenario}=passed",
            "apple_ios_rf_ledger=passed",
            "apple_ios_rf_post_dispose_silence=passed",
            "apple_ios_corebluetooth_rf=passed",
        ]
    if any(lines.count(line) != 1 for line in required) or any(
        "=failed" in line for line in lines
    ):
        raise RuntimeError(
            "incomplete/failed native smoke result; App pass alone is insufficient"
        )


def cleanup_fixture(fixture: DirectFixture | None, boot_nonce: bytes | None) -> None:
    if fixture is None:
        return
    try:
        if fixture._mutation_allowed and not fixture._stopping:
            fixture._transport.require()
            plan = fixture._fixture_session
            if plan is None:
                raise RuntimeError("fixture cleanup requires this attempt's session")
            fault = FaultController(fixture._session, plan.session_id)
            limits = fault.require_idle()
            if boot_nonce is None or limits.boot_nonce != boot_nonce:
                raise RuntimeError(
                    "fixture boot changed/unproved; refusing cleanup mutation"
                )
            fixture._disconnect_connected_peers()
            limits = fault.require_idle()
            if limits.boot_nonce != boot_nonce:
                raise RuntimeError(
                    "fixture boot changed during cleanup; refusing mutation"
                )
            if limits.peer_present:
                raise RuntimeError("fixture cleanup could not prove peer retirement")
            fixture._gap.stop_advertising()
    finally:
        # Retirement above owns all mutations. stop now only closes this fixture's
        # transport/resources; it must not retry on a failed or changed boot.
        fixture._mutation_allowed = False
        fixture.stop()


def acquire_ios_exclusion(
    device: str, attempt: Path, report: dict[str, Any]
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Share the actual RN scan lock; radio ownership is checked, never adopted."""
    from tools.react_native.ios_radio_lease import IosRadioLease
    from tools.react_native.run_ios_rf_scan import LOCK

    # Construction resolves the real radio owner's normalized paths but acquires
    # nothing. Its restoration-bound finish protocol is not a native smoke lease.
    radio = IosRadioLease(device, attempt.name, attempt / "radio-not-owned.json")
    pending = radio.lock.with_name(radio.lock.name + ".restoration-pending")
    evidence = report["device_exclusion"] = {
        "rn_lock_path": str(LOCK),
        "rn_lock_acquired": False,
        "rn_lock_released": False,
        "radio_owner_path": str(radio.lock),
        "radio_restoration_path": str(pending),
        "radio_lease_acquired": False,
        "scope": "shared-RN-scan-mkdir; radio-owner-check-only, not a reservation or global phone lock",
    }
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.mkdir()
    except FileExistsError as error:
        raise RuntimeError(
            f"RN iOS owner already occupies {LOCK}; refusing takeover"
        ) from error
    inode = LOCK.stat().st_dev, LOCK.stat().st_ino
    evidence["rn_lock_acquired"] = True

    def require() -> None:
        info = LOCK.lstat()
        if LOCK.is_symlink() or (info.st_dev, info.st_ino) != inode:
            raise RuntimeError("shared RN iOS lock ownership changed")
        occupied = [
            str(path) for path in (radio.lock, pending) if os.path.lexists(path)
        ]
        evidence["radio_conflicts"] = occupied
        if occupied:
            raise RuntimeError(
                "iOS radio owner/restoration already occupies " + ", ".join(occupied)
            )

    def release() -> None:
        info = LOCK.lstat()
        if LOCK.is_symlink() or (info.st_dev, info.st_ino) != inode:
            raise RuntimeError("refusing to release a foreign RN iOS lock")
        LOCK.rmdir()  # Empty, still our inode; never clear a stale/foreign owner.
        evidence["rn_lock_released"] = True

    return require, release


def run(
    platform: str,
    device: str,
    port: str,
    contract: Path,
    profile: Path,
    app: Path,
    scenario: str,
    *,
    timeout: float = 240,
    success_app: str = "keep",
) -> Path:
    if platform not in {"ios", "macos"} or scenario not in {"io", "notification"}:
        raise ValueError("explicit ios/macos and io/notification required")
    if success_app not in {"keep", "close-owned"} or (
        success_app == "close-owned" and platform != "ios"
    ):
        raise ValueError("success_app must be keep, or explicit iOS close-owned")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 300
    ):
        raise ValueError("native smoke timeout must be finite in [1, 300]")
    if not port or port != port.strip():
        raise ValueError("explicit fixture port required")
    if platform == "ios":
        device = canonical_physical_udid(device)
    elif device != socket.gethostname():
        raise ValueError("macOS --device must equal this host's hostname")
    if sys.platform != "darwin":
        raise RuntimeError("Apple native HIL requires the real Mac host")
    digest = fingerprint(app)
    config = apple_profile(profile, "10", "ab")
    attempt = WORK / str(uuid.uuid4())
    attempt.mkdir(parents=True, mode=0o700)
    print(f"apple_native_hil_evidence={attempt}", flush=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "platform": platform,
        "device": device,
        "scenario": scenario,
        "artifact_sha256": digest,
        "primary_error": None,
        "cleanup_errors": [],
        "cleanup_failures": [],
        "failure": None,
        "success_app_policy": success_app,
        "permission_mutation_attempted": False,
        "radio_mutation_attempted": False,
        "ui_automation": "not-checked; not required by this native smoke",
    }
    stage = "artifact-preflight"
    fixture: DirectFixture | None = None
    fixture_boot: bytes | None = None
    native: NativeSmoke | None = None
    primary: BaseException | None = None
    release_ios_exclusion: Callable[[], None] = lambda: None
    coordination = attempt / "coordination"
    coordination.mkdir(mode=0o700)
    lock_root = WORK / "locks"
    lock_root.mkdir(exist_ok=True)
    with (lock_root / hashlib.sha256((platform + device).encode()).hexdigest()).open(
        "a+b"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            snapshot = attempt / app.name
            shutil.copytree(app, snapshot)
            if fingerprint(snapshot) != digest or fingerprint(app) != digest:
                raise RuntimeError("prebuilt App changed while snapshotting")
            native = NativeSmoke(platform, device, snapshot, attempt)
            if scenario == "notification":
                report["notification_stimuli"] = native.notification_stimuli
                report["notification_ack_scope"] = (
                    "validated BTP SET_VALUE response only; not RF TX or delivery"
                )
                report["post_disable_value_derivation"] = (
                    "flip first-byte low bit; preserve remaining bytes and length"
                )
            native.deadline = time.monotonic() + timeout
            stage = "phone-ownership"
            if platform == "ios":
                native.require_device_owner, release_ios_exclusion = (
                    acquire_ios_exclusion(device, attempt, report)
                )
                native.require_device_owner()
            stage = "fixture-inputs"
            plan = prepare_session(
                contract,
                profile,
                attempt / "fixture-session",
                port=port,
                service_uuid=config.serviceUuid,
                session_id=attempt.name,
            )
            require_multifunction(plan)
            config = apple_profile(plan.profile_path, "10", "ab")
            spec = load_session_profile(plan.contract, plan.profile_path)
            report.update(
                run_id=plan.session_id,
                fixture_request=str(plan.request_path),
                fixture_receipt=str(plan.receipt_path),
                resource_identity=plan.resource_identity,
                contract_sha256=plan.contract_sha256,
                profile_sha256=plan.profile_sha256,
                fixture_pid=os.getpid(),
                fixture_pgid=os.getpgid(0),
                artifact_source=str(app.absolute()),
                artifact_snapshot=str(snapshot),
                scan_filter={
                    "service_uuid": spec.service_uuid,
                    "name_prefix": spec.local_name,
                },
                timeout_seconds=timeout,
                scope="one-normal-native-smoke-session-not-matrix-or-phase-exit",
            )
            stage = "device-preflight"
            if platform == "ios":
                require_ready_unlocked_device(
                    device, cwd=ROOT, timeout=native.budget(30)
                )
                native.refuse_existing()
            stage = "fixture-admission"
            fixture = DirectFixture(port, plan.profile_path, fixture_session=plan)
            fault = FaultController(fixture._session, plan.session_id)

            def pin_fixture_boot() -> None:
                nonlocal fixture_boot
                native.budget()
                native.require_device_owner()
                limits = fault.require_idle()
                if limits.peer_present:
                    raise RuntimeError(
                        "fixture has an unowned peer before native launch"
                    )
                fixture_boot = limits.boot_nonce
                report["fixture_boot_nonce"] = fixture_boot.hex()

            handles = fixture.start(before_mutations=pin_fixture_boot)
            receipt_bytes = plan.receipt_path.read_bytes()

            def guard() -> None:
                assert native is not None and fixture is not None
                native.budget()
                native.require_device_owner()
                fixture._transport.require()
                verify_receipt(plan, fixture_identity=(os.getpid(), os.getpgid(0)))
                if plan.receipt_path.read_bytes() != receipt_bytes:
                    raise RuntimeError("live fixture receipt changed")

            def mutation_guard() -> None:
                guard()
                limits = fault.require_idle(timeout=native.budget(1))
                if limits.boot_nonce != fixture_boot:
                    raise RuntimeError(
                        "fixture boot changed before mutation; refusing new boot operation"
                    )
                native.budget()

            stage = "native-install"
            native.install(mutation_guard)
            stage = "native-launch"
            native.launch(
                config,
                scenario,
                coordination,
                mutation_guard,
                name_prefix=spec.local_name,
            )
            stage = "native-runtime"
            sent = False
            disabled = False
            while True:
                guard()
                if native.identity(coordination):
                    if scenario == "notification":
                        if (
                            not sent
                            and native.pull("notification-enabled", coordination)
                            is not None
                        ):
                            native.trigger_notification(
                                fixture,
                                handles["updates"],
                                spec.notification_value,
                                coordination,
                                mutation_guard,
                            )
                            sent = True
                            report["notification_value_hex"] = (
                                spec.notification_value.hex()
                            )
                        if (
                            not disabled
                            and native.pull("notification-disabled", coordination)
                            is not None
                        ):
                            if not sent:
                                raise RuntimeError(
                                    "notification disabled before host value trigger"
                                )
                            native.trigger_notification(
                                fixture,
                                handles["updates"],
                                spec.notification_value,
                                coordination,
                                mutation_guard,
                                post_disable=True,
                            )
                            native.release_disable(coordination, mutation_guard)
                            disabled = True
                    text = native.pull("result.txt", coordination)
                    if text is not None:
                        (attempt / "result.txt").write_text(text, encoding="utf-8")
                        stage = "native-result"
                        validate_result(text, native, config, scenario)
                        if scenario == "notification" and not (sent and disabled):
                            raise RuntimeError(
                                "result missing host notification/disable bridge"
                            )
                        break
                if native.process is not None and native.process.poll() is not None:
                    raise RuntimeError("native probe exited without a valid result")
                time.sleep(min(0.1, native.budget()))
            stage = "fixture-retirement"
            retirement = time.monotonic() + 5
            while True:
                limits = fault.require_idle()
                if limits.boot_nonce != fixture_boot:
                    raise RuntimeError("fixture boot changed during native smoke")
                if not limits.peer_present:
                    break
                guard()
                if time.monotonic() >= retirement:
                    raise TimeoutError(
                        "Peripheral peer remained after native disconnect/close"
                    )
                time.sleep(0.1)
            guard()
            report["peripheral_disconnected"] = True
            report["status"] = "passed"
        except BaseException as error:  # noqa: BLE001 - preserve primary through owned cleanup
            primary = error
            report["primary_error"] = f"{type(error).__name__}: {error}"
            report["failure"] = {
                "stage": stage,
                "exception_type": type(error).__name__,
                "detail": str(error),
                "root_cause": "undetermined; stage is not a BLE/TCC diagnosis",
            }
        finally:
            for label, action in (
                (
                    "native",
                    lambda: (
                        native.cleanup(
                            coordination,
                            passed=report["status"] == "passed",
                            close_on_success=success_app == "close-owned",
                        )
                        if native
                        else None
                    ),
                ),
                ("fixture", lambda: cleanup_fixture(fixture, fixture_boot)),
                ("ios-exclusion", release_ios_exclusion),
            ):
                try:
                    action()
                except BaseException as cleanup:  # noqa: BLE001 - independent cleanup failures
                    report["cleanup_errors"].append(
                        f"{label}: {type(cleanup).__name__}: {cleanup}"
                    )
                    report["cleanup_failures"].append(
                        {
                            "stage": "cleanup." + label,
                            "exception_type": type(cleanup).__name__,
                            "detail": str(cleanup),
                        }
                    )
            if native is not None:
                report["native_identity"] = native.boot
                report["native_launch_attempted"] = native.launch_attempted
                report["native_identity_verified"] = native.boot is not None
                report["native_termination"] = {
                    "attempted": native.termination_attempted,
                    "confirmed": native.termination_confirmed,
                    "scope": "this run iOS PID/executable/fresh boot or macOS Popen child; not resource-zero",
                }
                if scenario == "notification":
                    report["notification_marker_released"] = (
                        native.notification_marker_released
                    )
            if report["cleanup_errors"]:
                report["status"] = "failed"
                if report["failure"] is None:
                    report["failure"] = report["cleanup_failures"][0]
            try:
                (attempt / "result.json").write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as publication:
                if primary is None:
                    for error in report["cleanup_errors"]:
                        publication.add_note(error)
                    raise
                primary.add_note(f"native result publication: {publication}")
    if primary is not None:
        for error in report["cleanup_errors"]:
            primary.add_note(error)
        raise primary
    if report["cleanup_errors"]:
        raise RuntimeError("; ".join(report["cleanup_errors"]))
    return attempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("ios", "macos"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--scenario", choices=("io", "notification"), required=True)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument(
        "--success-app",
        choices=("keep", "close-owned"),
        default="keep",
        help="iOS only: explicitly authorize closing this run App after success; never a pre-existing App",
    )
    args = parser.parse_args()
    try:
        path = run(
            args.platform,
            args.device,
            args.port,
            args.fixture_contract,
            args.fixture_profile,
            args.app,
            args.scenario,
            timeout=args.timeout,
            success_app=args.success_app,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        print(f"apple-native-hil:failed {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", []):
            print(f"cleanup: {note}", file=sys.stderr)
        return 2
    print(f"apple-native-hil:passed evidence={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
