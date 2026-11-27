"""Shared J-Link/BTP helpers and the historical local-HEX iPhone controller.

This module owns only the non-default, no-flash/no-reset DK control path. It
uses the maintained BTP protocol/session/transport modules and the diagnostic
profile/image/read-hold modules. It does not import ``spikes`` or ``nrftest``.

Attestation and the read-only doctor still use this module's probe helpers.
The iPhone controller is historical and requires its matching old host/profile;
current pending reload uses the live-attested multifunction gate. Do not retire
these shared helpers with the archived CLI or infer current RF from old runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from tests.peripherals.archive.nrf_att_read_hold import pending_read_ledger as ledger
from tests.peripherals.archive.nrf_att_read_hold.image import WORK as IMAGE_WORK
from tests.peripherals.archive.nrf_att_read_hold.profile import (
    load_diagnostic_contract,
    load_diagnostic_profile,
)
from tests.peripherals.archive.nrf_att_read_hold.read_hold import (
    ABORT,
    GATT_SERVICE,
    RELEASE,
    HoldState,
    Identity,
    ReadHold,
)
from tests.peripherals.nrf52840 import provider
from tests.peripherals.nrf52840.btp_commands import (
    GAP,
    GATT,
    BtpCommandError,
    CoreCommands,
)
from tests.peripherals.nrf52840.btp_protocol import (
    BtpFrameError,
    BtpSessionError,
)
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.btp_transport import (
    BtpTransportError,
    FramedTransport,
)
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from tests.peripherals.nrf52840.gatt_commands import (
    GattCommands,
)
from tests.peripherals.nrf52840.profile import ProfilePlan, provision
from tests.peripherals.nrf52840.resource_lease import DeviceLease, owned_lease
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport, SerialPort
from tools.react_native import run_ios_rf_scan

ROOT = Path(__file__).resolve().parents[4]
PROFILE = ROOT / "tests/conformance/scenarios/blehub-nrf-att-read-hold-v1.json"
HOST_BUILD = ROOT / "tests/consumers/react-native-internal/ios/build"
APP = HOST_BUILD / "device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app"
RUNTIME_RUNNER = ROOT / "tools/react_native/run_ios_device_runtime.py"
WORK = IMAGE_WORK
FIXTURE_PID = provider.PID_FILE
RN_LOCK = run_ios_rf_scan.LOCK
_DIAGNOSTIC_CONTRACT = load_diagnostic_contract()
SERVICE_UUID = _DIAGNOSTIC_CONTRACT.service_uuid
HOST_EXPECTED_LOCAL_NAME = _DIAGNOSTIC_CONTRACT.local_name
HOLD_SECONDS = 5.0
FRESH_READ_TIMEOUT_SECONDS = 12.0
CAPTURE_SECONDS = 85
RUNNER_TIMEOUT_SECONDS = CAPTURE_SECONDS + 30
READ_HOLD_COMMAND_MASK = sum(1 << opcode for opcode in (0x08, 0x24, 0x25, 0x26, 0x27))
UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UDID_PATTERN = re.compile(r"[0-9A-Fa-f-]{25,40}\Z")
PROBE_SERIAL_PATTERN = re.compile(r"[0-9]+\Z")


class PortInfo(Protocol):
    device: str
    vid: int | None
    serial_number: str | None
    pid: int | None
    location: str | None


class DiagnosticError(RuntimeError):
    """A required diagnostic identity, ownership, or lifecycle boundary failed."""


@dataclass(frozen=True)
class ProbeIdentity:
    """The USB identity shared by all VCOM ports exposed by one J-Link."""

    pid: int
    location: str
    ports: tuple[str, ...]


@dataclass(frozen=True)
class AttemptPaths:
    """UUID-separated retained logs for one bounded attempt."""

    run_id: str
    attempt_log: Path
    runner_log: Path
    notification_name: str


class LockClaim:
    """An atomic directory lock that removes only the owner that created it."""

    def __init__(self, path: Path, token: str) -> None:
        self.path: Path = path
        self.token: str = token
        self._released: bool = False

    def release(self) -> None:
        if self._released:
            return
        owner = self.path / ".owner"
        try:
            if owner.read_text(encoding="ascii") != self.token:
                raise DiagnosticError(f"RN lock ownership changed: {self.path}")
            owner.unlink()
            self.path.rmdir()
        except FileNotFoundError as error:
            raise DiagnosticError(
                f"RN lock disappeared before release: {self.path}"
            ) from error
        self._released = True


def validate_udid(udid: str) -> None:
    """Require a caller-supplied physical device identifier, never auto-select."""
    if UDID_PATTERN.fullmatch(udid) is None:
        raise ValueError("--udid must be an explicit physical device identifier")


def validate_probe_serial(serial_number: str) -> None:
    """Require the numeric USB serial printed on the selected J-Link."""
    if PROBE_SERIAL_PATTERN.fullmatch(serial_number) is None:
        raise ValueError("--probe-serial must be an explicit numeric J-Link USB serial")


def _canonical_probe_serial(serial_number: str) -> str:
    """Compare decimal USB serials without treating formatting zeros as identity."""
    validate_probe_serial(serial_number)
    return serial_number.lstrip("0") or "0"


def _probe_serial_matches(actual: object, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    try:
        return _canonical_probe_serial(actual) == _canonical_probe_serial(expected)
    except ValueError:
        return False


def validate_host_contract(profile_path: Path = PROFILE) -> None:
    """Refuse the current host/profile local-name mismatch before hardware work."""
    profile = load_diagnostic_profile(profile_path)
    if profile.service_uuid != SERVICE_UUID:
        raise DiagnosticError("diagnostic profile service UUID is not FD10")
    if profile.local_name != HOST_EXPECTED_LOCAL_NAME:
        raise DiagnosticError(
            f"pending-read reload profile mismatch: RN App.tsx expects {HOST_EXPECTED_LOCAL_NAME!r}, "
            f"but the diagnostic profile advertises {profile.local_name!r}; align the maintained host before RF"
        )


def validate_signed_app(app: Path = APP) -> None:
    """Require an existing signed app; never build, sign, or install one here."""
    if not app.is_dir() or app.is_symlink():
        raise DiagnosticError(f"built signed iOS host app is missing or linked: {app}")
    codesign = shutil.which("codesign")
    if codesign is None:
        raise DiagnosticError("codesign is unavailable; refusing an unverified app")
    try:
        subprocess.run(
            [codesign, "--verify", "--deep", "--strict", str(app)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiagnosticError(
            f"signed iOS host app verification failed: {app}"
        ) from error


def claim_lock(path: Path = RN_LOCK) -> LockClaim:
    """Claim the existing RN fixture lock without taking over stale ownership."""
    token = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as error:
        raise DiagnosticError(
            f"RN iOS fixture lock is already present: {path}"
        ) from error
    try:
        with (path / ".owner").open("x", encoding="ascii") as owner:
            owner.write(token)
    except Exception:
        with contextlib.suppress(OSError):
            path.rmdir()
        raise
    return LockClaim(path, token)


def new_attempt_paths(work: Path = WORK) -> AttemptPaths:
    """Create no files, but return one UUID namespace for all retained logs."""
    run_id = str(uuid.uuid4())
    if uuid.UUID(run_id).hex != run_id.replace("-", ""):
        raise DiagnosticError("generated run ID is not canonical")
    stem = f"rn-pending-read-reload-{uuid.UUID(run_id).hex}"
    return AttemptPaths(
        run_id,
        work / f"{stem}.attempt.log",
        work / f"{stem}.runner.log",
        f"dev.blehub.rn.reload.{run_id}",
    )


def _list_ports() -> list[PortInfo]:
    try:
        from serial.tools import list_ports
    except ImportError as error:
        raise DiagnosticError(
            "pyserial is required only for the opt-in DK runner"
        ) from error
    return list(list_ports.comports())


def verify_probe(serial_number: str) -> ProbeIdentity:
    """Require one SEGGER USB identity and one or two distinct J-Link VCOMs."""
    canonical_serial = _canonical_probe_serial(serial_number)
    ports = [
        port
        for port in _list_ports()
        if getattr(port, "vid", None) == 0x1366
        and _probe_serial_matches(
            getattr(port, "serial_number", None), canonical_serial
        )
        and str(getattr(port, "device", "")).startswith("/dev/cu.usbmodem")
    ]
    if not 1 <= len(ports) <= 2:
        raise DiagnosticError(
            f"expected one SEGGER probe with 1-2 VCOM ports for {serial_number}; found {len(ports)}"
        )
    identities = {
        (getattr(port, "pid", None), getattr(port, "location", None)) for port in ports
    }
    names = tuple(sorted(str(port.device) for port in ports))
    if len(identities) != 1 or len(set(names)) != len(names):
        raise DiagnosticError("SEGGER VCOM ports do not identify one USB device")
    pid, location = identities.pop()
    if not isinstance(pid, int) or not isinstance(location, str) or not location:
        raise DiagnosticError(
            "SEGGER VCOM has incomplete USB product/location identity"
        )
    return ProbeIdentity(pid, location, names)


@contextlib.contextmanager
def open_btp_port(port: str) -> Generator[SerialPort, None, None]:
    """Open one exclusive 115200 J-Link VCOM with hardware flow control."""
    try:
        import serial
    except ImportError as error:
        raise DiagnosticError(
            "pyserial is required only for the opt-in DK runner"
        ) from error
    try:
        connection = serial.Serial(
            port=port,
            baudrate=115200,
            timeout=1,
            write_timeout=1,
            rtscts=True,
            exclusive=True,
        )
    except (OSError, ValueError) as error:
        raise DiagnosticError(
            f"cannot exclusively open J-Link VCOM {port}: {error}"
        ) from error
    try:
        yield cast(SerialPort, cast(object, connection))
    finally:
        connection.close()


def _is_btp_probe_error(error: BaseException) -> bool:
    return isinstance(
        error,
        (
            TimeoutError,
            EOFError,
            BtpFrameError,
            BtpSessionError,
            BtpTransportError,
            BtpCommandError,
        ),
    )


def detect_btp_port(
    serial_number: str,
    requested_port: str | None = None,
    *,
    lease: DeviceLease | None = None,
) -> tuple[str, ProbeIdentity]:
    """Hold physical ownership before probing any VCOM; callers may borrow it."""
    with owned_lease("jlink", serial_number, purpose="BTP detection", lease=lease):
        return _detect_btp_port(serial_number, requested_port)


def _detect_btp_port(
    serial_number: str, requested_port: str | None
) -> tuple[str, ProbeIdentity]:
    """Select exactly one exclusive VCOM that answers maintained BTP Core."""
    probe = verify_probe(serial_number)
    found: list[str] = []
    for candidate in probe.ports:
        with open_btp_port(candidate) as connection:
            try:
                session = BtpSession(FramedTransport(SerialByteTransport(connection)))
                services = CoreCommands(session).read_supported_services()
            except Exception as error:
                if not _is_btp_probe_error(error):
                    raise
                continue
            required_services = 1 | (1 << GAP) | (1 << GATT)
            if services & required_services == required_services:
                found.append(candidate)
    if len(found) != 1:
        raise DiagnosticError(
            f"expected exactly one BTP VCOM on J-Link {serial_number}; "
            f"validated {found} among {probe.ports}"
        )
    if requested_port is not None and found[0] != requested_port:
        raise DiagnosticError(
            f"--port {requested_port} is not the unique BTP responder {found[0]}"
        )
    if verify_probe(serial_number) != probe:
        raise DiagnosticError("J-Link USB identity changed during BTP VCOM detection")
    return found[0], probe


def configure_btp(session: BtpSession) -> None:
    """Attach/register GAP/GATT and require the private read-hold handlers."""
    core = CoreCommands(session)
    command_masks: dict[int, int] = {}
    for service in (GAP, GATT):
        try:
            command_mask = core.read_supported_commands(service)
        except BtpCommandError:
            core.register_service(service)
            command_mask = core.read_supported_commands(service)
        command_masks[service] = command_mask
    if command_masks[GATT] & READ_HOLD_COMMAND_MASK != READ_HOLD_COMMAND_MASK:
        raise DiagnosticError(
            "diagnostic GATT service is missing one or more private read-hold commands "
            f"(mask=0x{command_masks[GATT]:x}, required=0x{READ_HOLD_COMMAND_MASK:x})"
        )


def terminal_hold_state(hold: ReadHold, terminal: HoldState, deadline: float) -> None:
    """Wait for one exact firmware terminal state under a monotonic deadline."""
    allowed = {
        HoldState.RELEASED: (HoldState.RELEASE_REQUESTED,),
        HoldState.ABORTED: (HoldState.ABORT_REQUESTED,),
        HoldState.EXPIRED: (HoldState.WAITING,),
    }[terminal]
    while time.monotonic() < deadline:
        state = hold.status().state
        if state == terminal:
            return
        if state not in allowed:
            raise DiagnosticError(
                f"hold reached {state.name} instead of {terminal.name}"
            )
        time.sleep(0.05)
    raise TimeoutError(f"hold did not reach {terminal.name} before its deadline")


def gap_events(session: BtpSession) -> list[tuple[int, bytes]]:
    """Drain and validate peer-redacted GAP connection events from this owner."""
    events: list[tuple[int, bytes]] = []
    while (event := session.pop_event()) is not None:
        if event.service != GAP or event.opcode not in (
            GAP_DEVICE_CONNECTED,
            GAP_DEVICE_DISCONNECTED,
        ):
            continue
        if len(event.payload) < 7:
            raise DiagnosticError("truncated BTP GAP peer identity")
        events.append((event.opcode, event.payload[:7]))
    return events


def wait_expired_and_disconnected(
    hold: ReadHold,
    session: BtpSession,
    peer: bytes,
    waiting_at: float,
    process: subprocess.Popen[str],
    attempt_log: Path,
) -> None:
    """Require old expiry and its matching GAP disconnect before re-advertising."""
    disconnected = 0
    while time.monotonic() < waiting_at + HOLD_SECONDS + 2:
        inspect_log(attempt_log, process)
        state = hold.status().state
        for opcode, address in gap_events(session):
            if opcode == GAP_DEVICE_DISCONNECTED and address == peer:
                disconnected += 1
            else:
                raise DiagnosticError("unexpected or uncorrelated old-peer GAP event")
        if disconnected > 1:
            raise DiagnosticError("duplicate old-peer GAP disconnect")
        if state == HoldState.EXPIRED and disconnected == 1:
            return
        if state not in (HoldState.WAITING, HoldState.EXPIRED):
            raise DiagnosticError(f"old hold reached {state.name} instead of EXPIRED")
        time.sleep(0.05)
    raise TimeoutError("old hold did not expire and report its matching GAP disconnect")


def inspect_log(log: Path, process: subprocess.Popen[str]) -> list[str]:
    """Fail promptly on a child failure while preserving the attempt log."""
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    lines = ledger.markers(text)
    if any(":failed:" in line or ":close-rejected:" in line for line in lines):
        raise DiagnosticError(f"RN reported a pending-read failure; inspect {log}")
    if process.poll() is not None and process.returncode != 0:
        raise DiagnosticError(
            f"RN console exited {process.returncode} before completion; inspect {log}"
        )
    return lines


def wait_marker(
    log: Path,
    process: subprocess.Popen[str],
    expression: str,
    deadline: float,
) -> None:
    """Require one marker before a monotonic deadline and reject duplicates."""
    while time.monotonic() < deadline:
        lines = inspect_log(log, process)
        matches = [line for line in lines if re.fullmatch(expression, line)]
        if len(matches) > 1:
            raise DiagnosticError(f"duplicate RN marker {expression}")
        if matches:
            return
        if process.poll() is not None:
            raise DiagnosticError(f"RN console exited before {expression}: {log}")
        time.sleep(0.05)
    raise TimeoutError(f"missing RN marker {expression}: {log}")


def post_notification(
    xcrun: str,
    udid: str,
    name: str,
    log: Path,
    process: subprocess.Popen[str],
    stage: str,
    deadline: float,
    run_id: str,
) -> None:
    """Post exactly one UUID-scoped reload/arm notification at its barrier."""
    if stage not in ("reload", "arm"):
        raise ValueError(f"unknown notification stage: {stage}")
    lines = inspect_log(log, process)
    ready = "external-ready" if stage == "reload" else "arm-ready"
    received = (
        "notification-received" if stage == "reload" else "arm-notification-received"
    )
    ready_marker = ledger.MODE + ":" + ready
    received_marker = ledger.MODE + ":" + received
    if lines.count(ready_marker) != 1 or lines.count(received_marker) != 0:
        raise DiagnosticError(
            f"{stage} notification is not at its unique ready barrier"
        )
    native = ledger.native_boundaries(lines, run_id=run_id, complete=stage == "arm")
    entered = ledger.one(
        lines,
        ledger.MODE + ":" + (ledger.OLD if stage == "reload" else ledger.AWAITING_ARM),
    )[0]
    accepted = native["read-accepted"][0]
    if accepted >= entered:
        raise DiagnosticError(
            "native read acceptance did not precede old read dispatch"
        )
    if stage == "reload" and any(
        event in native for event in ("dispose-returned", "late-submit-rejected")
    ):
        raise DiagnosticError("old native cleanup occurred before reload post")
    if stage == "arm":
        cleanup = ledger.one(lines, ledger.MODE + ":generation=1:cleanup-started")[0]
        if (
            not cleanup
            < native["dispose-returned"][0]
            < native["late-submit-rejected"][0]
            < lines.index(ready_marker)
        ):
            raise DiagnosticError("native cleanup markers are out of order before arm")
    if entered >= lines.index(ready_marker):
        raise DiagnosticError(
            f"{stage} notification ready before its matching read boundary"
        )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{stage} notification missed its deadline")
    subprocess.run(
        [
            xcrun,
            "devicectl",
            "device",
            "notification",
            "post",
            "--device",
            udid,
            "--name",
            name,
            "--timeout",
            "12",
            "--json-output",
            "-",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=min(18, remaining),
    )
    wait_marker(log, process, received_marker, deadline)
    if inspect_log(log, process).count(received_marker) != 1:
        raise DiagnosticError(f"duplicate {stage} notification receipt")


def reject_stale_controls(
    session: BtpSession, hold: ReadHold, old: Identity, fresh: Identity
) -> None:
    """Require old RELEASE/ABORT identities to leave the fresh ARMED hold unchanged."""
    if fresh.sequence == old.sequence or fresh.handle != old.handle:
        raise DiagnosticError("fresh hold reused the old identity")
    for opcode in (RELEASE, ABORT):
        response = session.request(
            service=GATT_SERVICE, opcode=opcode, payload=old.encode()
        )
        if response.opcode != 0 or response.payload != b"\x01":
            raise DiagnosticError("firmware accepted stale RELEASE or ABORT")
        if hold.status().state != HoldState.ARMED:
            raise DiagnosticError("stale control changed the fresh hold")


def _device_is_unlocked(xcrun: str, udid: str) -> None:
    result = subprocess.run(
        [
            xcrun,
            "devicectl",
            "device",
            "info",
            "lockState",
            "--device",
            udid,
            "--timeout",
            "15",
            "--json-output",
            "-",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )
    decoded = json.loads(result.stdout)
    if not isinstance(decoded, dict) or not isinstance(decoded.get("result"), dict):
        raise DiagnosticError("devicectl lock-state response is malformed")
    state = decoded["result"]
    if state.get("passcodeRequired") or not state.get("unlockedSinceBoot"):
        raise DiagnosticError("unlock the iPhone before the pending-read diagnostic")


def _stop_owned_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        # Do not hang the controller after SIGKILL; the caller retains the
        # fixture lock if the child is still alive and requires inspection.
        process.wait(timeout=5)


def _prepare_profile(gatt: GattCommands) -> tuple[ProfilePlan, dict[str, int]]:
    """Clear the owned dynamic DB while disconnected, then provision the diagnostic profile."""
    profile = load_diagnostic_profile(PROFILE)
    # The pinned Tester rejects this BTP command while any LE connection exists.
    # It clears only the dynamic GATT server, not firmware or the DK itself.
    gatt.reset_server()
    handles = provision(gatt, profile)
    target = next(
        characteristic
        for characteristic in profile.characteristics
        if characteristic.role == "read-write"
    )
    attributes = gatt.get_attributes(attribute_uuid=target.uuid)
    if (
        len(attributes) != 1
        or attributes[0].handle != handles["read-write"]
        or attributes[0].permissions != target.permissions
    ):
        raise DiagnosticError(
            "diagnostic read-write handle or permissions did not match"
        )
    return profile, handles


def run(
    udid: str,
    probe_serial: str,
    confirmed_sha: str,
    port: str | None = None,
) -> None:
    """Retired v1 entry: no lease, serial, native command or image mutation."""
    raise DiagnosticError(
        "legacy read-hold device entry is unavailable; use attested pending reload v2 (no v1 fallback)"
    )
