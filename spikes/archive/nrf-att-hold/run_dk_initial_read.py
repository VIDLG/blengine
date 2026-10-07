"""Isolated iPhone RN first-read feasibility probe against the diagnostic DK image.

This is not the RN RF I/O gate: the existing rf-io app attempts later reads that
are intentionally not authorized after the firmware's one-shot release.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import serial
from flash_dk import validate_image, verify_probe
from read_hold import ABORT, GATT_SERVICE, RELEASE, HoldState, Identity, ReadHold

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


from tests.peripherals.nrf52840.btp_commands import (  # noqa: E402
    GAP,
    GATT,
    BtpCommandError,
    CoreCommands,
)
from tests.peripherals.nrf52840.btp_protocol import BtpFrameError, BtpSessionError  # noqa: E402
from tests.peripherals.nrf52840.btp_session import BtpSession  # noqa: E402
from tests.peripherals.nrf52840.btp_transport import (  # noqa: E402
    BtpTransportError,
    FramedTransport,
)
from tests.peripherals.nrf52840.gap_commands import (  # noqa: E402
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GapCommands,
)
from tests.peripherals.nrf52840.gatt_commands import GattCommands, uuid_to_le_bytes  # noqa: E402
from tests.peripherals.nrf52840.profile import load_profile, provision  # noqa: E402
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport  # noqa: E402

SERVICE = "fd100000-0000-4000-8000-000000000000"

PROFILE = ROOT / "tests/conformance/scenarios/blehub-nrf-indication-v2.json"
APP = (
    ROOT
    / "tests/consumers/react-native-internal/ios/build"
    / "device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app"
)
WORK = ROOT / ".work/nrf-att-hold-dk"
FIXTURE_WORK = ROOT / ".work/peripheral/nrf52840"
FIXTURE_PID = FIXTURE_WORK / "fixture.pid"
LOCK = FIXTURE_WORK / "rn-ios-scan.lock"
READ_MARKER = "rf:step:read-initial:passed:"


def initial_read_evidence(log: str, service: str = SERVICE) -> str:
    """Require exactly one JS-validated first-read event/terminal marker before failure."""
    matching = [
        line for line in log.splitlines() if "BLEHUB_NITRO_RUNTIME " in line and READ_MARKER in line
    ]
    if len(matching) != 1:
        raise RuntimeError(f"expected one initial-read terminal marker, found {len(matching)}")
    line = matching[0]
    for field in (
        "commandId=9010",
        f"serviceUuid={service}",
        "value=00",
        "connectionId=9007",
        "gattSessionId=9009",
    ):
        if field not in line:
            raise RuntimeError(f"initial-read marker missing {field}")
    prefix = log[: log.index(line)]
    if "BLEHUB_NITRO_RUNTIME rf-io:failed:" in prefix:
        raise RuntimeError("RN failed before the initial read completed")
    return line.strip()


def failed_read_evidence(log: str, service: str = SERVICE) -> str:
    """Require the exact RN read command to terminal as an ATT permission error."""
    lines = [line for line in log.splitlines() if "BLEHUB_NITRO_RUNTIME " in line]
    discoveries = [
        line
        for line in lines
        if "rf:step:gatt-discovery:passed:" in line
        and f"serviceUuid={service}" in line
        and "connectionId=9007" in line
        and "gattSessionId=9009" in line
    ]
    failures = [
        line
        for line in lines
        if "rf:step:read-initial:failed:commandCompleted.error:9010:permission:" in line
    ]
    if len(discoveries) != 1 or len(failures) != 1:
        raise RuntimeError("missing exact discovery or initial-read permission terminal")
    if log.index(discoveries[0]) >= log.index(failures[0]):
        raise RuntimeError("read failed before matching GATT discovery")
    if any("rf:step:read-initial:passed:" in line or "rf-io:passed:" in line for line in lines):
        raise RuntimeError("a failed one-shot read cannot also be a success")
    return failures[0].strip()


def new_attempt_paths(action: str) -> tuple[Path, Path]:
    """Keep every diagnostic run's attempt and runner logs, including failures."""
    stem = f"rn-first-read-{action}-{uuid.uuid4().hex}"
    return WORK / f"{stem}.attempt.log", WORK / f"{stem}.runner.log"


def runtime_exit_reason(console: Path, attempt: Path) -> str:
    """Distinguish a locked iPhone from a missing RF read without discarding logs."""
    output = console.read_text(encoding="utf-8") if console.is_file() else ""
    if "BSErrorCodeDescription = Locked" in output:
        return "iPhone is locked; unlock it in Mirroring before rerunning (no RF started)"
    return f"iOS runtime exited before WAITING; inspect {console} and {attempt}"


def buffered_gap_events(session: BtpSession) -> tuple[int, int]:
    """Report peer-redacted Peripheral connection events already read by this BTP owner."""
    connected = disconnected = 0
    while (event := session.pop_event()) is not None:
        if event.service == GAP and event.opcode == GAP_DEVICE_CONNECTED:
            connected += 1
        elif event.service == GAP and event.opcode == GAP_DEVICE_DISCONNECTED:
            disconnected += 1
    return connected, disconnected


def terminal_hold_state(hold: ReadHold, terminal: HoldState, deadline: float) -> None:
    """Wait for the exact firmware terminal state with a finite monotonic deadline."""
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
            raise RuntimeError(f"hold reached {state.name} instead of {terminal.name}")
        time.sleep(0.05)
    raise TimeoutError(f"hold did not reach {terminal.name} before the deadline")


def verify_new_generation(session: BtpSession, hold: ReadHold, old: Identity) -> Identity:
    """Reject stale controls against a fresh arm, then abort the unused arm."""
    fresh = hold.arm(old.handle)
    try:
        if fresh.sequence == old.sequence or hold.status().state != HoldState.ARMED:
            raise RuntimeError("new hold generation did not reach ARMED")
        for opcode in (RELEASE, ABORT):
            response = session.request(service=GATT_SERVICE, opcode=opcode, payload=old.encode())
            if response.opcode != 0 or response.payload != b"\x01":
                raise RuntimeError("firmware accepted a stale hold control")
            if hold.status().state != HoldState.ARMED:
                raise RuntimeError("stale control changed the new hold state")
        return fresh
    finally:
        if hold.status().state == HoldState.ARMED:
            hold.abort()
            terminal_hold_state(hold, HoldState.ABORTED, time.monotonic() + 2)


def open_btp_port(port: str) -> serial.Serial:
    """Open one bounded, exclusive J-Link VCOM with the DK's UART configuration."""
    return serial.Serial(
        port=port,
        baudrate=115200,
        timeout=1,
        write_timeout=1,
        rtscts=True,
        exclusive=True,
    )


def detect_btp_port(serial_number: str) -> tuple[str, tuple[int, str, tuple[str, ...]]]:
    """Select exactly one responding BTP VCOM from one verified SEGGER USB device."""
    probe = verify_probe(serial_number)
    found: list[str] = []
    for candidate in probe[2]:
        try:
            with open_btp_port(candidate) as connection:
                session = BtpSession(FramedTransport(SerialByteTransport(connection)))
                services = CoreCommands(session).read_supported_services()
                required = 1 | (1 << GAP) | (1 << GATT)
                if services & required == required:
                    found.append(candidate)
        except serial.SerialException as error:
            raise RuntimeError(
                f"cannot exclusively inspect J-Link VCOM {candidate}: {error}"
            ) from error
        except (
            TimeoutError,
            EOFError,
            BtpFrameError,
            BtpSessionError,
            BtpTransportError,
            BtpCommandError,
        ):
            # A non-BTP VCOM is expected to time out; never treat raw bytes as readiness.
            continue
    if len(found) != 1:
        raise RuntimeError(
            f"expected exactly one BTP VCOM on J-Link {serial_number}; "
            f"validated {found} among {probe[2]}"
        )
    if verify_probe(serial_number) != probe:
        raise RuntimeError("J-Link USB identity changed during BTP VCOM detection")
    return found[0], probe


def reconnect_control_session(
    previous: BtpSession, port: str, owners: contextlib.ExitStack
) -> BtpSession:
    """Close the old BTP owner before exclusively reopening the same VCOM."""
    previous.close()
    connection = owners.enter_context(open_btp_port(port))
    return BtpSession(FramedTransport(SerialByteTransport(connection)))


def run(
    udid: str,
    serial_number: str,
    confirmed_sha: str,
    port: str | None = None,
    action: str = "release",
) -> None:
    """Own one BTP session, bounded hold, RN process and cross-boundary evidence."""
    if not re.fullmatch(r"[0-9A-Fa-f-]{25,40}", udid):
        raise ValueError("--udid must be an explicit physical device identifier")
    if action not in ("release", "abort", "expire", "transport-loss"):
        raise ValueError("--action must be release, abort, expire or transport-loss")
    validate_image(confirmed_sha)
    if not APP.is_dir():
        raise RuntimeError(f"previously built iOS host app is missing: {APP}")
    if FIXTURE_PID.exists():
        raise RuntimeError(f"refusing to take over running/stale fixture: {FIXTURE_PID}")
    FIXTURE_WORK.mkdir(parents=True, exist_ok=True)
    LOCK.mkdir()  # Atomic; never remove a lock that we did not create.
    process: subprocess.Popen[str] | None = None
    try:
        selected_port, probe = detect_btp_port(serial_number)
        if port is not None and port != selected_port:
            raise RuntimeError(
                f"--port {port} is not the uniquely responding BTP VCOM {selected_port}"
            )
        port = selected_port
        print(f"diagnostic_btp_port={port}:probe_location={probe[1]}", flush=True)
        WORK.mkdir(parents=True, exist_ok=True)
        log, console = new_attempt_paths(action)
        if log.exists() or console.exists():
            raise RuntimeError("diagnostic attempt log identity already exists")
        print(f"diagnostic_attempt_log={log}", flush=True)
        xcrun = shutil.which("xcrun")
        if xcrun is None:
            raise RuntimeError("xcrun is unavailable")
        subprocess.run(
            [xcrun, "devicectl", "device", "install", "app", "--device", udid, str(APP)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        if verify_probe(serial_number) != probe:
            raise RuntimeError("J-Link USB identity changed before opening BTP session")
        with contextlib.ExitStack() as serial_owners:
            connection = serial_owners.enter_context(open_btp_port(port))
            session = BtpSession(FramedTransport(SerialByteTransport(connection)))
            core = CoreCommands(session)
            for service in (GAP, GATT):
                try:
                    core.read_supported_commands(service)
                except BtpCommandError:
                    core.register_service(service)
            gap = GapCommands(session)
            gatt = GattCommands(session)
            profile = load_profile(PROFILE)
            profile = dataclasses.replace(
                profile,
                service_uuid=SERVICE,
                characteristics=tuple(
                    dataclasses.replace(char, permissions=char.permissions | 0x40)
                    if char.role == "read-write"
                    else char
                    for char in profile.characteristics
                ),
            )
            hold = ReadHold(session)
            armed = False
            try:
                gatt.reset_server()
                handles = provision(gatt, profile)
                attributes = gatt.get_attributes(attribute_uuid=profile.characteristics[0].uuid)
                if (
                    len(attributes) != 1
                    or attributes[0].handle != handles["read-write"]
                    or attributes[0].permissions != 0x43
                ):
                    raise RuntimeError("diagnostic read-write handle/permissions did not match")
                for operation in (
                    lambda: gap.set_powered(True),
                    lambda: gap.set_connectable(True),
                    lambda: gap.set_discoverable(True),
                ):
                    # The advertising command below is authoritative.
                    with contextlib.suppress(BtpCommandError):
                        operation()
                gap.start_advertising(
                    {0x01: b"\x06", 0x07: uuid_to_le_bytes(SERVICE)},
                    {0x09: profile.local_name.encode("utf-8")},
                )
                identity = hold.arm(handles["read-write"])
                armed = True
                if hold.status().state != HoldState.ARMED:
                    raise RuntimeError("hold did not reach ARMED")
                stale_connected, stale_disconnected = buffered_gap_events(session)
                if stale_connected or stale_disconnected:
                    print(
                        f"btp_prelaunch_gap_events_ignored=connected:{stale_connected},"
                        f"disconnected:{stale_disconnected}",
                        flush=True,
                    )
                with console.open("x", encoding="utf-8") as output:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            str(ROOT / "tools/react_native/run_ios_device_runtime.py"),
                            udid,
                            str(log),
                            "--seconds",
                            "60",
                            "--rf-io-service-uuid",
                            SERVICE,
                        ],
                        cwd=ROOT,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    deadline = time.monotonic() + 55
                    while True:
                        if process.poll() is not None:
                            connected, disconnected = buffered_gap_events(session)
                            print(
                                f"btp_buffered_gap_events=connected:{connected},"
                                f"disconnected:{disconnected}",
                                flush=True,
                            )
                            raise RuntimeError(runtime_exit_reason(console, log))
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Central read did not enter WAITING in 55s")
                        snapshot = hold.status()
                        if snapshot.state == HoldState.WAITING:
                            waiting_at = time.monotonic()
                            if hold.status().state != HoldState.WAITING:
                                raise RuntimeError("hold left WAITING before release")
                            if action in ("expire", "transport-loss"):
                                if action == "transport-loss":
                                    session = reconnect_control_session(
                                        session, port, serial_owners
                                    )
                                    if verify_probe(serial_number) != probe:
                                        raise RuntimeError(
                                            "J-Link USB identity changed during BTP reconnect"
                                        )
                                    if connection.is_open:
                                        raise RuntimeError("old BTP serial owner stayed open")
                                    hold = ReadHold(session)
                                    gap = GapCommands(session)
                                    observed = hold.status()
                                    if observed.identity != identity or observed.state not in (
                                        HoldState.WAITING,
                                        HoldState.EXPIRED,
                                    ):
                                        raise RuntimeError(
                                            f"hold changed after reconnect: {observed}"
                                        )
                                    print(
                                        f"btp_owner_reopened=sequence:{identity.sequence},"
                                        f"handle:{identity.handle},state:{observed.state.name}"
                                    )
                                terminal_hold_state(hold, HoldState.EXPIRED, waiting_at + 7)
                                terminal = HoldState.EXPIRED
                            else:
                                if time.monotonic() - waiting_at >= 4:
                                    raise TimeoutError("unsafe hold control timing")
                                if action == "release":
                                    hold.release()
                                    terminal = HoldState.RELEASED
                                else:
                                    hold.abort()
                                    terminal = HoldState.ABORTED
                                terminal_hold_state(hold, terminal, waiting_at + 4)
                            print(
                                f"firmware_waiting_{terminal.name.lower()}="
                                f"sequence:{identity.sequence},handle:{identity.handle}"
                            )
                            break
                        if snapshot.state != HoldState.ARMED:
                            raise RuntimeError(f"unexpected hold state: {snapshot.state.name}")
                        time.sleep(0.05)
                    try:
                        process.wait(timeout=75)
                    except subprocess.TimeoutExpired as error:
                        raise RuntimeError(
                            "iOS runtime did not exit after bounded capture"
                        ) from error
                observed = log.read_text(encoding="utf-8")
                evidence = (
                    initial_read_evidence(observed)
                    if action == "release"
                    else failed_read_evidence(observed)
                )
                print(f"rn_initial_read_evidence={evidence}")
                if action != "release":
                    fresh = verify_new_generation(session, hold, identity)
                    print(
                        f"recovery_stale_controls_rejected=old:{identity.sequence},"
                        f"new:{fresh.sequence},state:ABORTED"
                    )
                print(
                    f"runtime_exit={process.returncode} (full rf-io is outside this one-shot probe)"
                )
                print(f"attempt_log={log}")
            finally:
                cleanup_errors: list[str] = []
                if armed:
                    try:
                        snapshot = hold.status()
                        if snapshot.state in (HoldState.ARMED, HoldState.WAITING):
                            hold.abort()
                            terminal_hold_state(hold, HoldState.ABORTED, time.monotonic() + 2)
                            print("hold_cleanup=ABORTED")
                        elif snapshot.state in (
                            HoldState.PREPARING,
                            HoldState.RELEASE_REQUESTED,
                            HoldState.ABORT_REQUESTED,
                        ):
                            raise RuntimeError(f"unsafe hold cleanup state: {snapshot.state.name}")
                    except Exception as error:
                        cleanup_errors.append(f"hold: {error}")
                try:
                    gap.stop_advertising()
                except Exception as error:
                    cleanup_errors.append(f"advertising: {error}")
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=70)  # Runner owns and bounds its devicectl child.
                    except subprocess.TimeoutExpired as error:
                        cleanup_errors.append(f"runtime still live: pid={process.pid}")
                        raise RuntimeError("; ".join(cleanup_errors)) from error
                if cleanup_errors:
                    raise RuntimeError("cleanup unverified: " + "; ".join(cleanup_errors))
    finally:
        if process is None or process.poll() is not None:
            LOCK.rmdir()
        else:
            print(f"runtime still live: retaining {LOCK}", file=sys.stderr)


def main() -> int:
    """Run the explicitly selected diagnostic-hardware feasibility experiment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    parser.add_argument("--probe-serial", required=True)
    parser.add_argument("--port", help="optional assertion against auto-detected BTP VCOM")
    parser.add_argument("--confirm-hex-sha256", required=True)
    parser.add_argument(
        "--action",
        choices=("release", "abort", "expire", "transport-loss"),
        default="release",
    )
    args = parser.parse_args()
    try:
        run(args.udid, args.probe_serial, args.confirm_hex_sha256, args.port, args.action)
    except (
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        serial.SerialException,
    ) as error:
        print(f"DK initial-read experiment failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
