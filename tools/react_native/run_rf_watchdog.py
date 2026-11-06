"""Host-controlled, live-attested RN watchdog RF experiment; never build or flash."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import provider
from tests.peripherals.nrf52840.btp_commands import (
    GAP,
    BtpCommandError,
)
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.btp_transport import ByteTransport, FramedTransport
from tests.peripherals.nrf52840.cancellation_control import Control
from tests.peripherals.nrf52840.doctor import NrfDoctor
from tests.peripherals.nrf52840.fault_control import (
    FaultController,
    Outcome,
    Phase,
    Ticket,
    Winner,
    cleanup_ready,
    encode_status,
    ticket_document,
)
from tests.peripherals.nrf52840.fixture_contract import (
    MULTIFUNCTION_CAPABILITIES,
    load_contract,
)
from tests.peripherals.nrf52840.fixture_session import (
    attest_session,
    load_session_profile,
    prepare_session,
    validate_fixture_inputs,
    verify_receipt,
)
from tests.peripherals.nrf52840.gap_commands import (
    AD_COMPLETE_LOCAL_NAME,
    AD_FLAGS,
    AD_SERVICE_UUID128,
    CONTROLLER_INFO,
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GapCommands,
)
from tests.peripherals.nrf52840.gatt_commands import (
    GattCommands,
    tester_characteristic_native_permissions,
    uuid_to_le_bytes,
)
from tests.peripherals.nrf52840.profile import ProfilePlan
from tests.peripherals.nrf52840.resource_lease import DeviceLease, port_lease
from tests.peripherals.nrf52840.serial_transport import open_serial
from tools.react_native import android_sdk, ios_sdk
from tools.react_native import watchdog_ledger as ledger
from tools.react_native.host_contract import BUNDLE_ID

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work/react-native/rf-watchdog"
SERVICE = "fd600000-0000-4000-8000-000000000000"
V2_CAPTURE_SECONDS = 8.0
V2_WATCHDOG_SECONDS = 16.0
V2_CLEANUP_SECONDS = 7.0


class WatchdogError(RuntimeError):
    """The host cannot prove the watchdog/late-response experiment safely."""


class LeasedTransport:
    """Check broker ownership before every byte-level serial operation."""

    def __init__(self, transport: ByteTransport, lease: DeviceLease) -> None:
        self.transport = transport
        self.lease = lease

    def require(self) -> None:
        self.lease.require(*self.lease.identity.split(":", 1))

    def read(self, timeout: float) -> bytes:
        self.require()
        return self.transport.read(timeout)

    def write(self, data: bytes) -> None:
        self.require()
        self.transport.write(data)

    def close(self) -> None:
        self.transport.close()


def validate_inputs(contract_path: Path, profile_path: Path) -> ProfilePlan:
    """Reject normal/foreign roles, capabilities and topology before control opens."""
    contract = load_contract(contract_path)
    if (
        contract.fixture_role != "multifunction-rf"
        or contract.capabilities != frozenset(MULTIFUNCTION_CAPABILITIES)
    ):
        raise WatchdogError(
            "requires v2 multifunction-rf role with exact eleven capabilities"
        )
    validate_fixture_inputs(
        contract_path, profile_path, service_uuid=contract.service_uuid
    )
    profile = load_session_profile(contract, profile_path)
    rw = next(c for c in profile.characteristics if c.role == "read-write")
    if rw.permissions != 0x03 or rw.initial_value != b"\x00":
        raise WatchdogError(
            "watchdog target requires ordinary RW permissions and initial 00"
        )
    return profile


def fingerprint(path: Path) -> str:
    """Hash the existing app bundle or APK, including relative bundle filenames."""
    digest = hashlib.sha256()
    if path.is_symlink():
        raise WatchdogError(f"app artifact is linked: {path}")
    paths = sorted(path.rglob("*")) if path.is_dir() else [path]
    for item in paths:
        if item.is_symlink():
            raise WatchdogError(f"app artifact contains a symlink: {item}")
        if item.is_file():
            if path.is_dir():
                digest.update(item.relative_to(path).as_posix().encode())
                digest.update(b"\x00")
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    if not path.exists():
        raise WatchdogError(f"existing app artifact missing: {path}")
    return digest.hexdigest()


class Runtime:
    """Own this attempt's console/logcat capture and app launch."""

    def __init__(
        self,
        platform: str,
        device: str,
        attempt: Path,
        guard: Callable[[], None],
        *,
        replace_app: bool = False,
        service_uuid: str = SERVICE,
        mode: str = "rf-watchdog",
    ) -> None:
        if mode not in ("rf-watchdog", "rf-cancellation"):
            raise WatchdogError("unknown standalone read-fault diagnostic")
        self.mode = mode
        self.control: Control | None = None
        self.expected_local_name: str | None = None
        self.ios_identity: Any = None
        self.ios_records: list[str] = []
        self.platform, self.device, self.attempt, self.guard = (
            platform,
            device,
            attempt,
            guard,
        )
        self.log = attempt / "runtime.log"
        self.ios_launch_json = self.log.with_suffix(".launch.json")
        self.ios_cleanup_receipt = self.log.with_suffix(".console-cleanup.json")
        self.process: subprocess.Popen[str] | None = None
        self.output: Any = None
        self.launched = False
        self.android_pid: str | None = None
        self.android_records: list[str] = []
        # Preserve the evidence report's path, now containing one raw stream.
        self.android_raw_log = attempt / "android-logcat-snapshots.log"
        self.android_raw_prefix = b""
        self.android_capture: subprocess.Popen[bytes] | None = None
        self.android_output: Any = None
        self.android_errors: Any = None
        self.replace_app = replace_app
        self.service_uuid = service_uuid

    def command(
        self, command: list[str], timeout: float = 15
    ) -> subprocess.CompletedProcess[str]:
        self.guard()
        sdk = android_sdk if self.platform == "android" else ios_sdk
        result = sdk.run_command(command, timeout=timeout, check=True)
        self.guard()
        with (self.attempt / "host-commands.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{command!r}\n{result.stdout}\n")
        return result

    def adb(
        self, *arguments: str, timeout: float = 15
    ) -> subprocess.CompletedProcess[str]:
        return self.command(android_sdk.adb_command(self.device, *arguments), timeout)

    def install(self, apk: Path | None) -> dict[str, Any]:
        self.guard()
        if self.platform == "android":
            replaced = False
            if android_sdk.runtime_alive(self.device):
                if not self.replace_app:
                    raise WatchdogError(
                        "refusing to take over a pre-existing Android RN process; use --replace-app explicitly"
                    )
                # install() is invoked only after live fixture proof/configuration.
                self.adb("shell", "am", "force-stop", "com.blehubnitrohost")
                if android_sdk.runtime_alive(self.device):
                    raise WatchdogError(
                        "explicit Android app replacement did not stop the known package"
                    )
                replaced = True
            self.guard()
            artifact: dict[str, Any] = {"kind": "existing-installed-app"}
            if apk is not None:
                artifact = {"path": str(apk), "sha256": fingerprint(apk)}
                self.command(
                    [
                        sys.executable,
                        "-m",
                        "tools.android.install_apk",
                        "--serial",
                        self.device,
                        "--apk",
                        str(apk),
                        "--device",
                        "physical",
                        "--log",
                        str(self.attempt / "apk-install.log"),
                    ],
                    120,
                )
            artifact["replaced_running_app"] = replaced
            self.adb("logcat", "-b", "main", "-c")
            self.guard()
            android_sdk.grant_bluetooth_permissions(self.device)
            self.guard()
            return artifact
        from tools.react_native.ios_device import require_ready_unlocked_device
        from tools.react_native.run_ios_rf_scan import APP

        self.command(["codesign", "--verify", "--deep", "--strict", str(APP)], 30)
        self.guard()
        require_ready_unlocked_device(self.device, cwd=ROOT)
        self.guard()
        artifact = {"path": str(APP), "sha256": fingerprint(APP)}
        self.command(
            ios_sdk.devicectl_install_app(
                self.device, APP, self.attempt / "app-install.json"
            ),
            120,
        )
        if self.control is None:
            raise WatchdogError(
                "iOS read-fault control metadata must precede installation"
            )
        from tools.react_native.rf_control_relay import IosRuntimeIdentity

        self.ios_identity = IosRuntimeIdentity(
            self.device, self.control, self.attempt / "app-install.json", self.log
        )
        return artifact

    def launch(self) -> None:
        self.guard()
        if self.control is None or not self.expected_local_name:
            raise WatchdogError(
                "standalone watchdog requires frozen ARM barrier metadata"
            )
        self.launched = True  # even a partial failed launch owns failure cleanup
        if self.platform == "android":
            output = self.adb(
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                "com.blehubnitrohost/.MainActivity",
                "--es",
                "blehub.runtimeMode",
                self.mode,
                "--es",
                "blehub.rfServiceUuid",
                self.service_uuid,
                "--es",
                "blehub.rfCancellationControl",
                json.dumps(asdict(self.control)),
                "--es",
                "blehub.rfExpectedLocalName",
                self.expected_local_name,
                timeout=20,
            ).stdout
            if re.search(r"Error:|Exception|Activity not started", output):
                raise WatchdogError(f"Android watchdog launch failed: {output}")
            self.android_pid = self.android_process_id()
            (self.attempt / "android-runtime-identity.json").write_text(
                json.dumps(
                    {
                        "device": self.device,
                        "package": "com.blehubnitrohost",
                        "pid": self.android_pid,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            self.start_android_capture()
            self.require_android_pid()
        else:
            self.ios_launch_json.unlink(missing_ok=True)
            self.ios_cleanup_receipt.unlink(missing_ok=True)
            self.output = (self.attempt / "runtime-runner.log").open(
                "w", encoding="utf-8"
            )
            if self.ios_identity is None:
                raise WatchdogError(
                    "iOS launch lacks the installed artifact/control identity"
                )
            self.ios_identity.begin_launch(time.monotonic(), time.monotonic() + 120)
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tools.react_native.run_ios_device_runtime",
                    self.device,
                    str(self.log),
                    "--seconds",
                    "120",
                    "--rf-watchdog-service-uuid"
                    if self.mode == "rf-watchdog"
                    else "--rf-cancellation-service-uuid",
                    self.service_uuid,
                    "--rf-control-json",
                    json.dumps(asdict(self.control)),
                    "--rf-expected-local-name",
                    self.expected_local_name,
                    "--keep-app",
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        self.guard()

    def wait_control_request(self, request: str, deadline: float) -> None:
        if self.control is None:
            raise WatchdogError("missing watchdog control metadata")
        expected = self.control.marker("ready", request)
        while True:
            text = self.snapshot()
            count = self.markers(text).count(expected)
            if count > 1:
                raise WatchdogError("duplicate watchdog control request")
            if time.monotonic() >= deadline:
                raise WatchdogError("watchdog control request deadline")
            if count == 1 and (
                self.ios_identity is None or self.ios_identity.ready_for_relay(text)
            ):
                return
            self.guard()
            time.sleep(0.05)

    def signal(self, phase: str) -> None:
        if self.control is None:
            raise WatchdogError("read-fault relay requires control metadata")
        if self.platform == "ios":
            from tools.react_native.rf_control_relay import relay_ios

            if self.ios_identity is None:
                raise WatchdogError("iOS relay requires a pinned runtime")
            self.snapshot()
            self.ios_identity.before_signal(phase)
            self.ios_identity.sent_signal(phase)
            relay_ios(self.device, self.control, phase)
            self.guard()
            return
        if self.android_pid is None:
            raise WatchdogError("watchdog relay requires the pinned Android runtime")
        from tools.react_native.rf_control_relay import relay_android

        self.guard()
        relay_android(self.device, self.android_pid, self.control, phase)
        self.guard()

    def start_android_capture(self) -> None:
        """Drain the pinned PID's main buffer continuously, without a Python pipe."""
        self.guard()
        if self.android_pid is None:
            raise WatchdogError("Android runtime PID was not pinned at launch")
        self.android_output = self.android_raw_log.open("wb")
        self.android_errors = (self.attempt / "android-logcat-stderr.log").open("wb")
        # logcat first drains the existing launch records, then follows new ones.
        # Direct file redirection preserves all raw bytes, including partial lines,
        # without waiting for snapshot polls or accumulating an in-memory ledger.
        self.android_capture = subprocess.Popen(
            android_sdk.adb_command(
                self.device,
                "logcat",
                "-b",
                "main",
                "--pid",
                self.android_pid,
                "-v",
                "threadtime",
                "*:V",
            ),
            stdin=subprocess.DEVNULL,
            stdout=self.android_output,
            stderr=self.android_errors,
        )
        self.require_android_capture()

    def require_android_capture(self) -> None:
        if self.android_capture is None:
            raise WatchdogError("Android logcat capture was not started")
        code = self.android_capture.poll()
        if code is not None:
            raise WatchdogError(
                f"Android logcat capture exited early (exit={code}); inspect {self.attempt}"
            )

    def android_process_id(self) -> str:
        pid = self.adb(
            "shell", "pidof", "com.blehubnitrohost", timeout=2
        ).stdout.strip()
        if re.fullmatch(r"[1-9][0-9]*", pid) is None:
            raise WatchdogError(
                "expected exactly one live PID for the owned Android RN package"
            )
        return pid

    def require_android_pid(self) -> None:
        if self.android_pid is None:
            raise WatchdogError("Android runtime PID was not pinned at launch")
        if self.android_process_id() != self.android_pid:
            raise WatchdogError("owned Android runtime PID changed during attempt")

    def android_marker_records(self, raw: str) -> list[str]:
        """Keep complete timestamped records, including identical duplicates."""
        records = []
        for line in raw.splitlines(keepends=True):
            if not line.endswith("\n") or ledger.PREFIX not in line:
                continue
            match = re.fullmatch(
                r"[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+\s+"
                r"(?P<pid>[1-9][0-9]*)\s+[1-9][0-9]*\s+[VDIWEFA]\s+[^:]+:\s*(?P<body>.*)\n",
                line,
            )
            if match is None:
                raise WatchdogError(
                    "Android runtime marker lacks a threadtime record header"
                )
            if not match.group("body").startswith(
                tuple(ledger.PREFIX + prefix for prefix in self.marker_prefixes)
            ):
                continue
            if match.group("pid") != self.android_pid:
                raise WatchdogError(
                    "watchdog log record does not belong to the pinned Android PID"
                )
            records.append(line)
        return records

    def snapshot(self, *, allow_exit: bool = False) -> str:
        self.guard()
        if self.platform == "android":
            self.require_android_pid()
            self.require_android_capture()
            captured = self.android_raw_log.read_bytes()
            self.require_android_pid()
            self.require_android_capture()
            if not captured.startswith(self.android_raw_prefix):
                raise WatchdogError(
                    "Android watchdog raw log records dropped or changed during attempt"
                )
            # A concurrent writer may leave a partial record (or UTF-8 character).
            # Keep those bytes in the artifact and require them to remain a prefix,
            # but interpret only newline-terminated records.
            raw = captured[: captured.rfind(b"\n") + 1].decode(
                "utf-8", errors="replace"
            )
            records = self.android_marker_records(raw)
            if records[: len(self.android_records)] != self.android_records:
                raise WatchdogError(
                    "Android watchdog marker records dropped or changed during attempt"
                )
            self.android_raw_prefix = captured
            self.android_records = records
            text = "".join(records)
            self.log.write_text(text, encoding="utf-8")
            if "BLEHUB_NITRO_RUNTIME failure:" in raw:
                raise WatchdogError("Android RN runtime reported failure")
        else:
            text = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
            if self.process is None:
                raise WatchdogError("iOS runtime was not started")
            # poll() reaps children: kill(pid, 0) alone treats zombies as alive.
            code = self.process.poll()
            if code is None and text and not text.endswith("\n"):
                # The console helper flushes byte chunks, not complete records.
                # Keep a partial JSON line in the artifact but do not interpret it
                # as an event/marker until its newline is visible in a later poll.
                text = text.rsplit("\n", 1)[0] + "\n" if "\n" in text else ""
            if code is not None and (
                code != 0 or not allow_exit or not self.completed(text)
            ):
                raise WatchdogError(
                    f"iOS runtime exited early (exit={code}); inspect {self.attempt}"
                )
        if (
            any(
                line.startswith(
                    (
                        f"{self.mode}:failed:",
                        f"{self.mode}:close:failed:",
                        f"{self.mode}:close:rejected:",
                        "rf-lab-suite:failed:",
                        "rf-lab-suite:flow-failed:",
                    )
                )
                for line in self.markers(text)
            )
            or "BLEHUB_NITRO_RUNTIME failure:" in text
        ):
            raise WatchdogError("RN watchdog reported failure")
        if self.ios_identity is not None:
            records = text.splitlines(keepends=True)
            if records[: len(self.ios_records)] != self.ios_records:
                raise WatchdogError("iOS control transcript changed or rewound")
            for line in records[len(self.ios_records) :]:
                self.ios_identity.observe(line)
            self.ios_records = records
            if self.completed(text):
                self.ios_identity.finish(text)
        if self.control is not None:
            ledger.verify_control(text, self.control)
        self.guard()
        return text

    @property
    def marker_prefixes(self) -> tuple[str, ...]:
        return (
            self.mode + ":",
            "rf-control:",
            "rf-host:",
            "rf-lab-suite:",
            *(("rf:step:", "rf:listener:") if self.mode == "rf-cancellation" else ()),
        )

    def markers(self, text: str) -> list[str]:
        """Preserve the owned lane and its control/host records, including duplicates."""
        return [
            body.strip()
            for line in text.splitlines()
            if ledger.PREFIX in line
            and (body := line.split(ledger.PREFIX, 1)[1]).startswith(
                self.marker_prefixes
            )
        ]

    def completed(self, text: str) -> bool:
        return (
            ledger.PASSED in ledger.markers(text)
            if self.mode == "rf-watchdog"
            else any(
                line.startswith("rf-cancellation:passed:")
                for line in self.markers(text)
            )
        )

    def stop(self, *, success: bool) -> None:
        try:
            self.stop_runtime(success=success)
        finally:
            # Capture cleanup must run even after fixture lease loss, PID drift,
            # failed launch or app-termination failure. Never rewrite the raw log.
            try:
                if self.android_capture is not None:
                    if self.android_capture.poll() is None:
                        try:
                            self.android_capture.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            self.android_capture.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.android_capture.kill()
                            self.android_capture.wait(timeout=5)
                    else:
                        self.android_capture.wait(timeout=5)
            finally:
                if self.android_output is not None:
                    self.android_output.close()
                if self.android_errors is not None:
                    self.android_errors.close()

    def stop_runtime(self, *, success: bool) -> None:
        finish_failed = False
        if success and self.process is not None and self.process.poll() is None:
            try:
                # The --keep-app helper owns its separate devicectl session and
                # detaches it itself. Let it reap that child before ending our owner.
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                finish_failed = True
                success = False
        if self.process is not None and self.process.poll() is None:
            # SIGINT raises KeyboardInterrupt in the Python helper, whose failure
            # unwind closes its separately-sessioned console. SIGTERM would bypass
            # that unwind and orphan the owned devicectl process.
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.output is not None:
            self.output.close()
        if self.launched and not success:
            # App termination is attempt-owned, independent of fixture lease loss.
            if self.platform == "android":
                if self.android_pid is None:
                    raise WatchdogError(
                        "no pinned Android PID; refusing unowned app termination"
                    )
                current = android_sdk.run_command(
                    android_sdk.adb_command(
                        self.device, "shell", "pidof", "com.blehubnitrohost"
                    ),
                    timeout=2,
                    check=False,
                )
                if current.returncode == 1 and not current.stdout.strip():
                    return  # the pinned app has already exited
                if (
                    current.returncode != 0
                    or current.stdout.strip() != self.android_pid
                ):
                    raise WatchdogError(
                        "Android PID changed or is unverifiable; refusing app termination"
                    )
                android_sdk.run_command(
                    android_sdk.adb_command(
                        self.device, "shell", "am", "force-stop", "com.blehubnitrohost"
                    ),
                    timeout=15,
                    check=True,
                )
            else:
                self.stop_ios_app()
        if finish_failed:
            raise WatchdogError(
                "iOS keep-app helper did not finish; attempt-owned runtime was stopped"
            )

    def ios_json(self, path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WatchdogError(
                f"unverifiable iOS launch/cleanup identity; refusing app termination: {path}"
            ) from error
        if not isinstance(document, dict):
            raise WatchdogError("invalid iOS identity JSON; refusing app termination")
        return document

    def ios_command_result(self, path: Path, command_type: str) -> dict[str, Any]:
        document = self.ios_json(path)
        info, result = document.get("info"), document.get("result")
        if not isinstance(info, dict) or not isinstance(result, dict):
            raise WatchdogError(
                "incomplete iOS command identity; refusing app termination"
            )
        arguments = info.get("arguments")
        if (
            info.get("outcome") != "success"
            or info.get("commandType") != command_type
            or not isinstance(arguments, list)
            or arguments[:4] != command_type.split(".")
            or arguments.count("--device") != 1
            or arguments[arguments.index("--device") + 1 :][:1] != [self.device]
            or not isinstance(result.get("deviceIdentifier"), str)
            or not result["deviceIdentifier"]
            or (command_type.endswith(".launch") and BUNDLE_ID not in arguments)
        ):
            raise WatchdogError(
                "foreign iOS command identity; refusing app termination"
            )
        return result

    def stop_ios_app(self) -> None:
        """Use console-owned cleanup, or terminate only the verified launch PID."""
        if self.process is None:
            return  # the local helper never started, so it could not launch an app
        receipt = self.ios_json(self.ios_cleanup_receipt)
        if (
            receipt.get("schema_version") != 1
            or receipt.get("device") != self.device
            or receipt.get("bundle_id") != BUNDLE_ID
            or type(receipt.get("helper_pid")) is not int
            or receipt["helper_pid"] != self.process.pid
            or receipt.get("launch_json") != str(self.ios_launch_json)
            or not isinstance(receipt.get("action"), str)
            or receipt["action"]
            not in {"signal-forwarded", "detached", "console-exited"}
        ):
            raise WatchdogError("foreign iOS cleanup receipt; refusing app termination")
        if receipt["action"] == "signal-forwarded":
            return  # the helper already signalled its attached app; no double kill
        launch = self.ios_command_result(
            self.ios_launch_json, "devicectl.device.process.launch"
        )
        process = launch.get("process")
        if (
            not isinstance(process, dict)
            or type(process.get("processIdentifier")) is not int
            or process["processIdentifier"] <= 0
            or not isinstance(process.get("executable"), str)
            or not process["executable"].startswith("file:///")
        ):
            raise WatchdogError(
                "no pinned iOS launch PID/executable; refusing app termination"
            )
        pid = process["processIdentifier"]
        processes_json = self.attempt / "ios-cleanup-processes.json"
        processes_json.unlink(missing_ok=True)
        ios_sdk.run_command(
            ios_sdk.devicectl_processes(self.device, processes_json, timeout=15),
            timeout=15,
            check=True,
        )
        current = self.ios_command_result(
            processes_json, "devicectl.device.info.processes"
        )
        rows = current.get("runningProcesses")
        if (
            current["deviceIdentifier"] != launch["deviceIdentifier"]
            or not isinstance(rows, list)
            or any(
                not isinstance(row, dict)
                or type(row.get("processIdentifier")) is not int
                or row["processIdentifier"] <= 0
                for row in rows
            )
        ):
            raise WatchdogError(
                "unverifiable iOS process list; refusing app termination"
            )
        matches = [row for row in rows if row["processIdentifier"] == pid]
        if not matches:
            return  # never select a later process by bundle or executable name
        if len(matches) != 1 or matches[0].get("executable") != process["executable"]:
            raise WatchdogError(
                "iOS launch PID identity changed; refusing app termination"
            )
        terminate_json = self.attempt / "ios-cleanup-terminate.json"
        terminate_json.unlink(missing_ok=True)
        ios_sdk.run_command(
            ios_sdk.devicectl_terminate_process(self.device, pid, terminate_json),
            timeout=30,
            check=True,
        )


class Experiment:
    """One synchronous BTP owner, with all waits guarded by the same device lease."""

    def __init__(
        self,
        session: BtpSession,
        profile: ProfilePlan,
        guard: Callable[[], None],
        report: dict[str, Any],
    ) -> None:
        self.session, self.profile, self.guard, self.report = (
            session,
            profile,
            guard,
            report,
        )
        self.gap, self.gatt = GapCommands(session), GattCommands(session)
        self.fault = (
            FaultController(session, report["session_id"])
            if report.get("session_id")
            else None
        )
        self.ticket: Ticket | None = None
        self.peer: bytes | None = None
        self.peers: set[bytes] = set()
        self.mutation_allowed = False
        self.connection_baseline_established = False
        self.runtime_started = False
        self.armed_at = 0.0

    def record(self, boundary: str, **fields: Any) -> None:
        if (
            boundary == "gap"
            and getattr(self, "_cleanup_only", False)
            and fields["opcode"] == GAP_DEVICE_CONNECTED
        ):
            raise WatchdogError("refusing a new GAP connection during watchdog cleanup")
        self.report.setdefault("boundaries", []).append(
            {
                "boundary": boundary,
                "monotonic": time.monotonic(),
                "utc": datetime.now(UTC).isoformat(),
                **fields,
            }
        )

    def pause(self, seconds: float = 0.05) -> None:
        self.guard()
        time.sleep(seconds)
        self.guard()

    def events(self, *, poll: bool = False) -> bool:
        self.guard()
        observed = False
        event = self.session.poll_event(0.05) if poll else None
        while event is not None or (event := self.session.pop_event()) is not None:
            observed = True
            if event.service == GAP and event.opcode in (
                GAP_DEVICE_CONNECTED,
                GAP_DEVICE_DISCONNECTED,
            ):
                if (
                    event.index != 0
                    or len(event.payload) < 7
                    or (
                        getattr(self, "strict_peer_events", False)
                        and (
                            type(event.service) is not int
                            or type(event.opcode) is not int
                            or type(event.index) is not int
                            or type(event.payload) is not bytes
                            or len(event.payload)
                            != (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
                            or event.payload[0] not in (0, 1, 2, 3)
                        )
                    )
                ):
                    raise WatchdogError("malformed GAP peer event")
                peer = event.payload[:7]
                self.record("gap", opcode=event.opcode, peer=peer.hex())
                if event.opcode == GAP_DEVICE_CONNECTED:
                    if self.peers:
                        raise WatchdogError("duplicate or concurrent GAP peer")
                    self.peers.add(peer)
                else:
                    if peer not in self.peers:
                        raise WatchdogError("uncorrelated GAP disconnect")
                    self.peers.remove(peer)
            event = None
        return observed

    def configure_v2(self) -> int:
        if not self.mutation_allowed or self.fault is None:
            raise WatchdogError(
                "live v2 proof/session required before fixture mutation"
            )
        initial = self.fault.require_idle()
        if initial.peer_present:
            raise WatchdogError("refusing to configure an unowned connected peer")
        NrfDoctor(self.session).ensure_services()
        try:
            self.gap.stop_advertising()
        except BtpCommandError as error:
            self.record("configure-stop-advertising-status", error=str(error))
        # Reuse the provider profile owner. It validates/reinitializes an exact
        # database, not GATT_RESET_SERVER and never a firmware reset/flash.
        result = NrfDoctor(self.session).run(self.profile)
        target = result.role_handles["read-write"]
        rw = next(c for c in self.profile.characteristics if c.role == "read-write")
        attrs = self.gatt.get_attributes(attribute_uuid=rw.uuid)
        if (
            len(attrs) != 1
            or attrs[0].handle != target
            or attrs[0].uuid != rw.uuid
            or attrs[0].permissions != tester_characteristic_native_permissions(0x03)
            or rw.permissions != 0x03
        ):
            raise WatchdogError("v2 watchdog requires the exact ordinary RW target")
        before = self.gatt.get_attribute_value(target)
        if (
            before.att_response != 0
            or before.value != rw.initial_value
            or before.value != b"\x00"
        ):
            raise WatchdogError("v2 watchdog initial readback mismatch")
        powered = self.controller_settings()
        try:
            self.gap.set_powered(True)
        except BtpCommandError as error:
            if (
                not str(error).endswith("BTP status 0x01")
                or not powered & 1
                or not self.controller_settings() & 1
            ):
                raise
        if not self.controller_settings() & 1:
            raise WatchdogError("GAP controller did not remain powered")
        self.gap.set_connectable(True)
        self.gap.set_discoverable(True)
        final = self.fault.require_idle()
        if final.peer_present or final.boot_nonce != initial.boot_nonce:
            raise WatchdogError("peer/boot changed during profile preparation")
        # The fresh RX snapshot establishes the baseline, not stale queued GAP
        # events and not a destructive reset. No future peer is adopted here.
        drain_deadline = time.monotonic() + 2
        for _ in range(256):
            self.guard()
            if time.monotonic() >= drain_deadline:
                raise WatchdogError("watchdog preparation event deadline")
            if self.session.pop_event() is None:
                break
        else:
            raise WatchdogError("watchdog preparation event backlog")
        self.peers.clear()
        self.connection_baseline_established = True
        self.record(
            "no-peer",
            method="fault-v2-get-limits-no-peer-baseline",
            boot_nonce=initial.boot_nonce.hex(),
        )
        self.record(
            "configured", target_handle=target, permissions=attrs[0].permissions
        )
        return target

    def controller_settings(self) -> int:
        self.guard()
        info = self.gap.read_controller_info()
        if len(info) != CONTROLLER_INFO.size:
            raise WatchdogError("GAP controller-info response has the wrong length")
        _, supported, current, _, _, _ = CONTROLLER_INFO.unpack(info)
        if not supported & 1:
            raise WatchdogError("GAP controller does not declare the POWERED setting")
        return current

    def advertise(self) -> None:
        self.gap.start_advertising(
            {
                AD_FLAGS: b"\x06",
                AD_SERVICE_UUID128: uuid_to_le_bytes(self.profile.service_uuid),
            },
            {AD_COMPLETE_LOCAL_NAME: self.profile.local_name.encode("utf-8")},
        )
        self.record("advertising")

    def run(self, runtime: Runtime, target: int) -> None:
        if self.fault is None:
            raise WatchdogError("v2 watchdog requires the frozen fixture session UUID")
        self.strict_peer_events = True
        self.runtime_started = True
        self.advertise()
        runtime.launch()
        self.run_connected(runtime, target)

    def run_connected(self, runtime: Any, target: int) -> None:
        """Use an existing runtime/barrier without launching, resetting or reprovisioning."""
        if self.fault is None:
            raise WatchdogError("v2 watchdog requires the frozen fixture session UUID")
        self.strict_peer_events = True
        self.runtime_started = True
        # Connected + discovered readiness is the only ARM entry. Never reserve
        # a ticket against a fabricated future connection before App launch.
        runtime.wait_control_request("arm", time.monotonic() + 60)
        drain_deadline = time.monotonic() + 2
        while self.events(poll=True):
            if time.monotonic() >= drain_deadline:
                raise WatchdogError("watchdog pre-ARM GAP drain deadline")
        text = runtime.snapshot()
        ready = re.findall(
            r"rf-watchdog:discovery-ready:connectionId=21007:gattSessionId=21009:characteristicId=([1-9][0-9]*)",
            text,
        )
        if len(ready) != 1 or len(self.peers) != 1:
            raise WatchdogError(
                "watchdog ARM requires exact connected/discovered route"
            )
        self.peer = next(iter(self.peers))
        rw = next(c for c in self.profile.characteristics if c.role == "read-write")
        before = self.gatt.get_attribute_value(target)
        self.guard()
        if (
            before.att_response != 0
            or before.value != rw.initial_value
            or before.value != b"\x00"
        ):
            raise WatchdogError("watchdog exact before value mismatch")
        self.armed_at = time.monotonic()
        self.ticket = self.fault.arm_read(target, before.value, self.peer).ticket
        armed = self.fault.status(self.ticket)
        self.events()
        if armed.phase is not Phase.ARMED or self.peers != {self.peer}:
            raise WatchdogError("watchdog ARM target/peer changed")
        self.record(
            "armed",
            monotonic=self.armed_at,
            **ticket_document(self.ticket),
            peer=self.peer.hex(),
            characteristic=ready[0],
            status_wire=encode_status(armed).hex(),
        )
        runtime.signal("armed")
        capture_deadline = self.armed_at + V2_CAPTURE_SECONDS
        runtime.wait_control_request("read-dispatched", capture_deadline)
        while True:
            self.guard()
            text = runtime.snapshot()
            self.events()
            status_started = time.monotonic()
            status = self.fault.status(self.ticket)
            if time.monotonic() >= capture_deadline:
                raise WatchdogError("watchdog capture deadline exceeded")
            if status.phase is Phase.HELD:
                break
            if status.phase is not Phase.ARMED:
                raise WatchdogError("read did not reach exact HELD")
            self.pause()
        dispatched = [
            m
            for line in ledger.markers(text)
            if (m := re.fullmatch(ledger.ACTIVE_DISPATCH, line))
        ]
        if (
            len(dispatched) != 1
            or dispatched[0].group(1) != ready[0]
            or self.peers != {self.peer}
        ):
            raise WatchdogError(
                "HELD receipt does not match the active native read route"
            )
        # Remaining time was sampled before UART delivered STATUS. Anchoring it
        # after the reply would renew the firmware budget by transport latency.
        held_at = status_started
        deadline = min(
            self.armed_at + V2_CAPTURE_SECONDS + V2_WATCHDOG_SECONDS,
            held_at + V2_WATCHDOG_SECONDS,
            held_at + status.remaining.hold_ms / 1000,
        )
        if time.monotonic() >= deadline:
            raise WatchdogError("watchdog HELD receipt already expired")
        self.record(
            "held",
            monotonic=held_at,
            **ticket_document(self.ticket),
            status_wire=encode_status(status).hex(),
            held_at=held_at,
        )
        runtime.signal("held")
        runtime.wait_control_request("release", deadline)
        text = runtime.snapshot()
        characteristic = ledger.release_gate(text)
        if characteristic != ready[0]:
            raise WatchdogError("native watchdog terminal route drift")
        while True:
            self.events(poll=True)
            status = self.fault.status(self.ticket)
            self.guard()
            if time.monotonic() >= deadline:
                raise WatchdogError("watchdog retained receipt/resource-zero deadline")
            if cleanup_ready(status) and not self.peers:
                break
            if self.peers and self.peers != {self.peer}:
                raise WatchdogError("foreign peer while watchdog owner retires")
            self.pause()
        if (
            status.winner is not Winner.DISCONNECT
            or status.outcome is not Outcome.DISCONNECTED_UNCOMMITTED
        ):
            raise WatchdogError("watchdog requires disconnected uncommitted receipt")
        self.record(
            "retired-after-watchdog",
            **ticket_document(self.ticket),
            peer=self.peer.hex(),
            status_wire=encode_status(status).hex(),
            native_route=characteristic,
        )
        # The barrier acknowledges settlement, not a remote commit or RELEASE.
        # STATUS remains legal here; RESOLVE/ABORT on this retired owner do not.
        runtime.signal("released")
        runtime.wait_control_request("recover", time.monotonic() + 10)
        for observation in range(2):
            if observation:
                self.pause(1)
            text = runtime.snapshot()
            self.events()
            if self.peers or ledger.release_gate(text) != characteristic:
                raise WatchdogError("old route/peer changed before recovery")
        self.record(
            "post-retirement-old-route-silence",
            observation_ms=1000,
            commands=["21010", "21011"],
        )
        self.advertise()
        recovery_boundary = len(self.report.get("boundaries", []))
        runtime.signal("recovery")
        deadline = time.monotonic() + 60
        while True:
            text = runtime.snapshot(allow_exit=True)
            self.events(poll=True)
            if time.monotonic() >= deadline:
                raise WatchdogError("watchdog recovery/close deadline")
            if ledger.PASSED in ledger.markers(text):
                self.report["ledger"] = ledger.verify_ledger(
                    text,
                    characteristic=characteristic,
                    service_uuid=self.profile.service_uuid,
                    integrated=True,
                )
                self.pause(1)
                text = runtime.snapshot(allow_exit=True)
                self.events(poll=True)
                self.report["ledger"] = ledger.verify_ledger(
                    text,
                    characteristic=characteristic,
                    service_uuid=self.profile.service_uuid,
                    integrated=True,
                )
                if runtime.control is None:
                    raise WatchdogError("missing final watchdog barrier owner")
                ledger.verify_control(text, runtime.control, complete=True)
                connections = [
                    row
                    for row in self.report["boundaries"][recovery_boundary:]
                    if row["boundary"] == "gap"
                    and row["opcode"] == GAP_DEVICE_CONNECTED
                ]
                if len(connections) != 1 or self.peers:
                    raise WatchdogError(
                        "recovery must reconnect and physically close one peer"
                    )
                return
            self.pause()

    def cleanup_v2(self) -> None:
        if not self.mutation_allowed:
            return
        if self.fault is None:
            raise WatchdogError("missing v2 controller")
        errors: list[str] = []
        self.report["physical_cleanup"] = {
            "hold_inactive": False,
            "advertising_stopped": False,
            "peer_state": "unknown",
            "radio_power": "unchanged",
        }
        deadline = time.monotonic() + V2_CLEANUP_SECONDS
        self._cleanup_only = True
        try:
            # A wrong/stale receipt never authorizes touching a newer owner or
            # consuming its queued GAP events. Inspect before any mutation.
            status = self.fault.status(self.ticket) if self.ticket is not None else None
            limits = self.fault.get_limits()
            if time.monotonic() >= deadline:
                raise WatchdogError("watchdog cleanup inspection deadline")
            if limits.peer_present and (
                self.ticket is None
                or (
                    limits.boot_nonce,
                    limits.connection_epoch,
                    limits.bearer_epoch,
                    limits.database_epoch,
                )
                != (
                    self.ticket.boot_nonce,
                    self.ticket.connection_epoch,
                    self.ticket.bearer_epoch,
                    self.ticket.database_epoch,
                )
                or self.peers != {limits.peer}
            ):
                raise WatchdogError("watchdog cleanup lacks ownership/no peers proof")
            self.events()
            if (
                status is not None
                and limits.peer_present
                and status.phase in (Phase.ARMED, Phase.HELD)
            ):
                self.fault.abort(self.ticket)
            if status is not None:
                while True:
                    status = self.fault.status(self.ticket)
                    if time.monotonic() >= deadline:
                        raise WatchdogError("watchdog cleanup retirement deadline")
                    if cleanup_ready(status):
                        break
                    self.pause()
            else:
                self.fault.require_idle()
        except Exception as error:
            self.report["physical_cleanup_errors"] = [f"fault-ownership: {error}"]
            raise WatchdogError(f"physical cleanup blocked: {error}") from error
        operations = [self.stop_advertising_for_cleanup]
        if self.runtime_started:
            operations.append(self.disconnect_known_peers)
        for operation in operations:
            try:
                self.guard()
                operation()
            except Exception as error:  # noqa: BLE001 - finish remaining owned cleanup
                errors.append(str(error))
        try:
            if self.ticket is not None:
                while True:
                    status = self.fault.status(self.ticket)
                    if time.monotonic() >= deadline:
                        raise WatchdogError("watchdog cleanup retirement deadline")
                    if cleanup_ready(status):
                        break
                    self.pause()
                self.report["physical_cleanup"]["status_wire"] = encode_status(
                    status
                ).hex()
            limits = self.fault.require_idle()
            self.events()
            if time.monotonic() >= deadline or limits.peer_present or self.peers:
                raise WatchdogError(
                    "watchdog final cleanup lacks an idle engine/no peers within deadline"
                )
            if self.ticket is not None and limits.boot_nonce != self.ticket.boot_nonce:
                raise WatchdogError("watchdog firmware boot changed during cleanup")
            self.report["physical_cleanup"]["hold_inactive"] = True
            self.report["physical_cleanup"]["peer_state"] = "no-live-or-tracked-peers"
            self.report["physical_cleanup"]["known_peers_remaining"] = []
        except Exception as error:  # noqa: BLE001 - preserve the primary separately from cleanup
            errors.append(f"fault-retirement: {error}")
        self.report["physical_cleanup_errors"] = errors
        if errors:
            raise WatchdogError("physical cleanup failed: " + "; ".join(errors))

    def stop_advertising_for_cleanup(self) -> None:
        try:
            self.gap.stop_advertising()
        except BtpCommandError as error:
            # An unsupported/no-op stop need not change native radio power, but
            # only a fresh controller snapshot with ADVERTISING clear is safe.
            current = self.controller_settings()
            self.record(
                "cleanup-stop-advertising-status",
                error=str(error),
                current_settings=current,
            )
            if current & (1 << 10):
                raise
        self.report["physical_cleanup"]["advertising_stopped"] = True

    def disconnect_known_peers(self) -> None:
        deadline = time.monotonic() + 3
        # A poll reads only one frame, including unrelated settings events. Drain
        # through an idle read before deciding which peers this owner knows about.
        while self.events(poll=True):
            if time.monotonic() >= deadline:
                raise WatchdogError("cleanup GAP event drain did not become idle")
        for peer in tuple(self.peers):
            self.guard()
            self.gap.disconnect(peer[1:7][::-1].hex(), peer[0])
        while True:
            observed = self.events(poll=True)
            if not self.peers and not observed:
                break
            if time.monotonic() >= deadline:
                raise WatchdogError(
                    "cleanup GAP disconnect was not observed for the known peer"
                )
            self.pause()
        if self.connection_baseline_established:
            self.report["physical_cleanup"]["peer_state"] = (
                "no-known-peers-after-live-no-peer-baseline"
            )
            self.record(
                "cleanup-peer-accounting",
                method="tracked-gap-disconnects-after-live-no-peer-baseline",
                known_peers=0,
            )


def run(
    platform: str,
    device: str,
    contract: Path,
    profile: Path,
    *,
    port: str | None = None,
    apk: Path | None = None,
    replace_app: bool = False,
    work: Path = WORK,
    diagnostic_mode: str = "rf-watchdog",
) -> Path:
    """Retain an attempt report on all outcomes; keep the successful app visible."""
    attempt = work / str(uuid.uuid4())
    attempt.mkdir(parents=True, mode=0o700)
    report: dict[str, Any] = {
        "attempt_id": attempt.name,
        "scenario": diagnostic_mode,
        "platform": platform,
        "device": device,
        "status": "failed",
        "runtime_log": str(attempt / "runtime.log"),
        "runner_sha256": fingerprint(Path(__file__)),
        "peripheral_hold_timeout_ms": 20000,
        "backend_watchdog_expected_ms": 10000,
    }
    if platform == "android":
        report["runtime_raw_log"] = str(attempt / "android-logcat-snapshots.log")
        report["runtime_identity"] = str(attempt / "android-runtime-identity.json")
    try:
        if diagnostic_mode not in ("rf-watchdog", "rf-cancellation"):
            raise WatchdogError("unknown standalone read-fault diagnostic")
        if platform not in {"ios", "android"} or not device or device != device.strip():
            raise WatchdogError("platform ios|android and explicit device are required")
        if (apk is not None or replace_app) and platform != "android":
            raise WatchdogError("--apk and --replace-app are Android-only")

        validated_profile = validate_inputs(contract, profile)
        if load_contract(contract).fixture_role != "multifunction-rf":
            raise WatchdogError("v2 watchdog requires exact multifunction-rf identity")
        selected = port if port is not None else provider._port()
        if not selected:
            raise WatchdogError(
                "explicit --port or configured provider port is required"
            )
        plan = prepare_session(
            contract,
            profile,
            attempt / "session",
            service_uuid=validated_profile.service_uuid,
            port=selected,
        )
        frozen_profile = validate_inputs(plan.root / "contract.json", plan.profile_path)
        report["fixture_request"] = str(plan.request_path)
        report["session_id"] = plan.session_id
        control = Control(
            plan.session_id,
            9 if diagnostic_mode == "rf-watchdog" else 8,
            diagnostic_mode,
            str(uuid.uuid4()),
        )
        report["control"] = asdict(control)
        report["capture_budget_seconds"] = V2_CAPTURE_SECONDS
        report["watchdog_boundary_seconds"] = V2_WATCHDOG_SECONDS
        with (
            port_lease(selected, purpose=f"BleHub RF watchdog {attempt.name}") as lease,
            ExitStack() as resources,
        ):
            lease.require(*plan.resource_identity.split(":", 1))
            raw = open_serial(selected)
            transport = LeasedTransport(raw, lease)
            session = BtpSession(FramedTransport(transport))
            resources.callback(session.close)
            if diagnostic_mode == "rf-cancellation":
                from tools.react_native.run_rf_cancellation import (
                    CancellationExperiment,
                )

                experiment_type = CancellationExperiment
            else:
                experiment_type = Experiment
            experiment = experiment_type(
                session,
                frozen_profile,
                transport.require,
                report,
            )
            runtime = Runtime(
                platform,
                device,
                attempt,
                transport.require,
                replace_app=replace_app,
                service_uuid=frozen_profile.service_uuid,
                mode=diagnostic_mode,
            )
            runtime.control = control
            runtime.expected_local_name = frozen_profile.local_name
            completed = False
            primary: BaseException | None = None
            try:
                attest_session(plan, session, lease=lease, port=selected)
                report["fixture_receipt"] = verify_receipt(
                    plan, fixture_identity=(os.getpid(), os.getpgid(0))
                )
                if (
                    frozenset(
                        report["fixture_receipt"]["installed_identity"]["capabilities"]
                    )
                    != plan.contract.capabilities
                ):
                    raise WatchdogError(
                        "installed image must declare exactly the validated contract capabilities"
                    )
                # Revalidate the frozen tuple rather than trusting mutable sources.
                validate_inputs(plan.root / "contract.json", plan.profile_path)
                experiment.mutation_allowed = True
                target = experiment.configure_v2()
                report["app_artifact"] = runtime.install(apk)
                experiment.run(runtime, target)
                transport.require()
                verify_receipt(plan, fixture_identity=(os.getpid(), os.getpgid(0)))
                completed = True
            except BaseException as error:
                primary = error
                raise
            finally:
                cleanup_errors = []
                for cleanup in (
                    experiment.cleanup_v2,
                    lambda: runtime.stop(success=completed),
                ):
                    try:
                        cleanup()
                    except Exception as error:  # noqa: BLE001 - preserve primary and finish cleanup
                        cleanup_errors.append(str(error))
                report["cleanup_errors"] = cleanup_errors
                if cleanup_errors:
                    if primary is not None:
                        primary.add_note("cleanup: " + "; ".join(cleanup_errors))
                    else:
                        raise WatchdogError("cleanup: " + "; ".join(cleanup_errors))
        report["status"] = "passed"
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["error_notes"] = getattr(error, "__notes__", [])
        raise
    finally:
        (attempt / "evidence.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"{diagnostic_mode}:report={attempt / 'evidence.json'}", flush=True)
    print(f"{diagnostic_mode}:passed", flush=True)
    return attempt / "evidence.json"


def main(diagnostic_mode: str = "rf-watchdog") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("ios", "android"))
    parser.add_argument("--device", required=True)
    parser.add_argument("--fixture-contract", required=True, type=Path)
    parser.add_argument("--fixture-profile", required=True, type=Path)
    parser.add_argument("--port")
    parser.add_argument("--apk", type=Path)
    parser.add_argument(
        "--replace-app",
        action="store_true",
        help="Android-only: explicitly stop the known RN package after live fixture proof",
    )
    args = parser.parse_args()

    def interrupted(_signal: int, _frame: object) -> None:
        raise InterruptedError("RF watchdog interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        run(
            args.platform,
            args.device,
            args.fixture_contract,
            args.fixture_profile,
            port=args.port,
            apk=args.apk,
            replace_app=args.replace_app,
            diagnostic_mode=diagnostic_mode,
        )
    except Exception as error:  # noqa: BLE001 - CLI reports every retained attempt failure
        print(f"{diagnostic_mode}:failed:{error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
