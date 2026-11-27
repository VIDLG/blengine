"""Opt-in production Android artifact-consumer smoke; never provisions firmware or radio."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, cast

from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.fault_control import FaultController
from tests.peripherals.nrf52840.fixture_session import (
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tests.peripherals.nrf52840.profile import load_profile
from tools.android import adb
from tools.android import run_instrumentation as instrumentation
from tools.android.state_control import AndroidRadioController
from tools.android_toolchain import adb_environment, select_test_device
from tools.file_digest import sha256_file

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work/validation/android-native-hil"
APP = "dev.blehub.ffi.runtime.consumer"
COMPONENT = APP + ".test/androidx.test.runner.AndroidJUnitRunner"
TEST_CLASS = APP + ".NativeHilSmokeTest"
NATIVE_HIL_MODE = "writes-v2"
APK_ROOT = ROOT / "tests/consumers/android/blehub/app/build/outputs/apk"
MAX_CAPTURE_BYTES = 1024 * 1024
PHASES = (
    "started",
    "initialized",
    "scanned",
    "connected",
    "discovered",
    "readback",
    "subscribed",
    "notified",
    "disconnected",
    "closed",
)


class SmokeCapture(io.TextIOBase):
    """Bound raw instrumentation output and relay only this attempt's ordered markers."""

    def __init__(
        self,
        log: TextIO,
        run_id: str,
        guard: Callable[[], None],
        notify: Callable[[], None],
    ) -> None:
        self.log, self.run_id, self.guard, self.notify = log, run_id, guard, notify
        self.phases: list[str] = []
        self.pid: int | None = None
        self.start_ticks: int | None = None
        self.pending = ""
        self.bytes_seen = 0

    def write(self, text: str) -> int:
        self.bytes_seen += len(text.encode("utf-8"))
        if self.bytes_seen > MAX_CAPTURE_BYTES:
            raise RuntimeError("native HIL instrumentation capture exceeded 1 MiB")
        self.log.write(text)
        self.log.flush()
        self.guard()
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            prefix = "INSTRUMENTATION_STATUS: native_hil="
            if not line.startswith(prefix):
                continue
            marker = line[len(prefix) :].rstrip("\r")
            fields = marker.split(":")
            if len(fields) not in (2, 4) or fields[0] != self.run_id:
                raise RuntimeError("foreign/malformed native HIL marker")
            phase = fields[1]
            if len(self.phases) >= len(PHASES) or phase != PHASES[len(self.phases)]:
                raise RuntimeError("duplicate/out-of-order native HIL phase: " + phase)
            if phase == "started":
                if len(fields) != 4 or any(
                    not re.fullmatch(r"[1-9][0-9]*", value) for value in fields[2:]
                ):
                    raise RuntimeError(
                        "native HIL must pin its Android PID/start ticks"
                    )
                self.pid, self.start_ticks = int(fields[2]), int(fields[3])
            elif len(fields) != 2:
                raise RuntimeError("unexpected native HIL marker fields")
            self.phases.append(phase)
            if phase == "subscribed":
                self.guard()
                self.notify()
        return len(text)

    def flush(self) -> None:
        if not self.log.closed:
            self.log.flush()

    def finish(self) -> None:
        if self.pending or tuple(self.phases) != PHASES:
            raise RuntimeError(
                "incomplete native HIL ledger; JUnit success alone is insufficient"
            )


def _command(device: str, log: TextIO, *args: str, absent_ok: bool = False) -> str:
    """Use the shared adb boundary without RN package/scenario ownership."""
    command = adb.adb_command(device, *args)
    result = adb.run_command(command, timeout=10)
    log.write(f"$ {' '.join(command)}\n{result.stdout}\n")
    log.flush()
    if len(result.stdout.encode()) > MAX_CAPTURE_BYTES:
        raise RuntimeError("Android command capture exceeded 1 MiB")
    if result.returncode and not (
        absent_ok and result.returncode == 1 and not result.stdout.strip()
    ):
        raise RuntimeError(f"Android command failed ({result.returncode}): {command}")
    return result.stdout.strip()


def _permissions(device: str, log: TextIO) -> None:
    api = _command(device, log, "shell", "getprop", "ro.build.version.sdk")
    if not api.isdecimal() or int(api) < 24:
        raise RuntimeError("native HIL requires Android API >= 24")
    required = (
        ("BLUETOOTH_SCAN", "BLUETOOTH_CONNECT")
        if int(api) >= 31
        else ("ACCESS_FINE_LOCATION",)
    )
    dump = _command(device, log, "shell", "dumpsys", "package", APP)
    for name in required:
        states = re.findall(
            rf"(?m)^\s*android\.permission\.{name}:\s*granted=(true|false)(?:[,\s]|$)",
            dump,
        )
        if states != ["true"]:
            raise RuntimeError(
                f"explicit permission setup required: {APP} permission {name}; no automatic grant"
            )


def _cleanup_app(device: str, log: TextIO, capture: SmokeCapture) -> None:
    current = _command(device, log, "shell", "pidof", APP, absent_ok=True)
    if not current:
        return
    if capture.pid is None or current != str(capture.pid):
        raise RuntimeError("unproved/foreign Android PID; refusing force-stop")
    stat = _command(device, log, "shell", "cat", f"/proc/{capture.pid}/stat")
    fields = stat.rsplit(") ", 1)
    if (
        len(fields) != 2
        or len(fields[1].split()) < 20
        or fields[1].split()[19] != str(capture.start_ticks)
    ):
        raise RuntimeError("Android PID was reused; refusing force-stop")
    _command(device, log, "shell", "am", "force-stop", APP)
    if _command(device, log, "shell", "pidof", APP, absent_ok=True):
        raise RuntimeError("attempt-owned Android process did not stop")


def run(
    device: str, port: str, contract: Path, profile: Path, *, timeout: int = 120
) -> Path:
    """Assert live identity before either install or launch; publish verdict after cleanup."""
    if type(timeout) is not int or not 100 <= timeout <= 180:
        raise ValueError("native HIL runtime timeout must be an integer in [100, 180]")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", device) or device.startswith("emulator-"):
        raise ValueError("an explicit physical Android serial is required")
    if not port.strip():
        raise ValueError("an explicit nRF fixture port is required")
    environment = adb_environment()
    environment["BLEHUB_ANDROID_SERIAL"] = device
    selected = select_test_device("physical", environment=environment)
    if selected.serial != device or selected.is_emulator:
        raise RuntimeError(
            "selected device does not match the explicit physical serial"
        )
    attempt = WORK / str(uuid.uuid4())
    attempt.mkdir(parents=True, mode=0o700)
    print(f"android_native_hil_evidence={attempt}", flush=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "device": device,
        "scope": "production-artifact-native-smoke-only",
        "native_hil_mode": NATIVE_HIL_MODE,
        "primary_error": None,
        "cleanup_errors": [],
        "artifacts": {},
    }
    primary: BaseException | None = None
    cleanup: list[str] = report["cleanup_errors"]
    fixture: DirectFixture | None = None
    phone: AndroidRadioController | None = None
    acquired = False
    launched = False
    capture: SmokeCapture | None = None
    with (attempt / "host.log").open("w", encoding="utf-8") as log:
        try:
            plan = prepare_session(
                contract,
                profile,
                attempt / "fixture-session",
                port=port,
                service_uuid=load_profile(profile).service_uuid,
                session_id=attempt.name,
            )
            spec = load_session_profile(plan.contract, plan.profile_path)
            roles = {item.role: item for item in spec.characteristics}
            initial = roles["read-write"].initial_value
            if not initial:
                raise ValueError(
                    "native smoke requires a nonempty read-write initial value"
                )
            written = bytes([initial[0] ^ 1]) + initial[1:]
            without_response = bytes([initial[0] ^ 2]) + initial[1:]
            report["read_write_values"] = {
                "initial_hex": initial.hex(),
                "written_hex": written.hex(),
                "without_response_hex": without_response.hex(),
            }
            report.update(
                run_id=plan.session_id, fixture_receipt=str(plan.receipt_path)
            )
            for name, source in (
                ("app.apk", APK_ROOT / "debug/app-debug.apk"),
                ("test.apk", APK_ROOT / "androidTest/debug/app-debug-androidTest.apk"),
            ):
                target = attempt / name
                shutil.copyfile(source, target)
                report["artifacts"][name] = sha256_file(target)
                if report["artifacts"][name] != sha256_file(source):
                    raise RuntimeError("APK changed while snapshotting")
            phone = AndroidRadioController(
                device, plan.session_id, attempt / "phone-observation.json"
            )
            phone.acquire()
            acquired = True
            phone.initial()  # Read-only: OFF is a failed precondition, never repaired.
            if _command(device, log, "shell", "pidof", APP, absent_ok=True):
                raise RuntimeError("consumer already running; refusing takeover")
            fixture = DirectFixture(port, plan.profile_path, fixture_session=plan)
            handles = fixture.start()
            receipt_identity = (os.getpid(), os.getpgid(0))
            receipt_bytes = plan.receipt_path.read_bytes()

            def guard() -> None:
                assert phone is not None and fixture is not None
                phone.require_owner()
                fixture._transport.require()
                verify_receipt(plan, fixture_identity=receipt_identity)
                if plan.receipt_path.read_bytes() != receipt_bytes:
                    raise RuntimeError("live fixture receipt changed")

            guard()
            use_root = instrumentation.has_root(device)
            for name, test_only in (("app.apk", False), ("test.apk", True)):
                guard()  # Must precede every device-facing install boundary.
                if sha256_file(attempt / name) != report["artifacts"][name]:
                    raise RuntimeError("snapshotted APK changed before install")
                instrumentation.install_apk(
                    device,
                    attempt / name,
                    test_only=test_only,
                    use_root=use_root,
                    log=log,
                )
            _permissions(device, log)

            def notify() -> None:
                assert fixture is not None
                fixture._gatt.set_value(handles["updates"], spec.notification_value)
                report["notification_trigger"] = {
                    "role": "updates",
                    "value_hex": spec.notification_value.hex(),
                    "btp_set_value_acknowledged": True,
                }

            arguments = {
                "class": TEST_CLASS,
                "nativeHil": NATIVE_HIL_MODE,
                "runId": plan.session_id,
                "serviceUuid": spec.service_uuid,
                "namePrefix": load_profile(plan.profile_path).local_name,
                "readWriteUuid": roles["read-write"].uuid,
                "updatesUuid": roles["updates"].uuid,
                "initialHex": initial.hex(),
                "writeHex": written.hex(),
                "writeWithoutResponseHex": without_response.hex(),
                "notificationHex": spec.notification_value.hex(),
            }
            command = adb.adb_command(device, "shell", "am", "instrument", "-w", "-r")
            for key, value in arguments.items():
                command.extend(("-e", key, value))
            command.append(COMPONENT)
            with (attempt / "instrumentation.log").open(
                "w", encoding="utf-8"
            ) as runtime:
                capture = SmokeCapture(runtime, plan.session_id, guard, notify)
                guard()
                if _command(device, log, "shell", "pidof", APP, absent_ok=True):
                    raise RuntimeError(
                        "consumer appeared before launch; refusing takeover"
                    )
                guard()  # Live lease/receipt asserted immediately before launch.
                launched = True
                instrumentation._run_instrumentation(
                    command,
                    log=cast(TextIO, capture),
                    timeout=timeout,
                    progress_label="native-hil",
                )
                capture.finish()
            guard()
            # Remote GAP retirement, not process death, is the cleanup precondition.
            deadline = time.monotonic() + 5
            fault = FaultController(fixture._session, plan.session_id)
            while fault.require_idle().peer_present:
                guard()
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Peripheral still has a peer after native disconnect"
                    )
                time.sleep(0.1)
            report["peripheral_disconnected"] = True
        except BaseException as error:  # noqa: BLE001 - retain interruption evidence before cleanup
            primary = error
            report["primary_error"] = f"{type(error).__name__}: {error}"
        finally:
            if capture is not None:
                report.update(
                    phases=capture.phases,
                    android_pid=capture.pid,
                    android_start_ticks=capture.start_ticks,
                )
            if launched and capture is not None:
                try:
                    assert phone is not None
                    phone.require_owner()
                    _cleanup_app(device, log, capture)
                except Exception as error:  # noqa: BLE001 - retain primary separately
                    cleanup.append(f"owned Android process: {error}")
            if fixture is not None:
                try:
                    fixture.stop()
                except Exception as error:  # noqa: BLE001 - continue releasing phone owner
                    cleanup.append(f"fixture stop: {error}")
            if acquired:
                assert phone is not None
                try:
                    # Never finish(): that may toggle radio.
                    cleanup.extend(phone.finish_observation(primary))
                except Exception as error:  # noqa: BLE001 - preserve evidence even if finalization fails
                    cleanup.append(f"phone final observation/release: {error}")
            report["status"] = "passed" if primary is None and not cleanup else "failed"
            (attempt / "result.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    if primary is not None:
        primary.add_note(f"native HIL evidence: {attempt}; cleanup_errors={cleanup}")
        raise primary
    if cleanup:
        raise RuntimeError(f"native HIL cleanup failed: {cleanup}; evidence={attempt}")
    print(f"android_native_hil=passed evidence={attempt}", flush=True)
    return attempt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="explicit physical adb serial")
    parser.add_argument(
        "--port", required=True, help="explicit leased nRF52840 BTP port"
    )
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    def interrupted(_signum: int, _frame: object) -> None:
        raise InterruptedError("native HIL interrupted by SIGTERM")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        run(args.device, args.port, args.contract, args.profile, timeout=args.timeout)
    except Exception as error:  # noqa: BLE001 - CLI reports retained evidence; never claims RF pass
        print(f"android_native_hil=failed: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
