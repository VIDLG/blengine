from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from tools.local_config import resolve_local_value

from .btp_session import BtpSession
from .btp_transport import FramedTransport
from .direct_fixture import serve
from .doctor import run_profile_doctor
from .serial_transport import open_serial

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE = (
    PROJECT_ROOT / "tests" / "conformance" / "scenarios" / "blehub-nrf-indication-v2.json"
)
WORK_ROOT = PROJECT_ROOT / ".work" / "peripheral" / "nrf52840"
PID_FILE = WORK_ROOT / "fixture.pid"
LOG_FILE = WORK_ROOT / "fixture.log"
READY_MARKER = "NRFTEST_GATT_FIXTURE_READY "
DEFAULT_READY_TIMEOUT = 45
DEFAULT_RF_TIMEOUT = 300


class Nrf52840Error(RuntimeError):
    pass


def _nrftest_root() -> Path:
    configured = resolve_local_value(
        "toolchains", "nrftest_root", "BLEHUB_NRFTEST_ROOT", None
    )
    if configured.value is None:
        raise Nrf52840Error(
            "nRF52840 provider requires toolchains.nrftest_root in ignored "
            + "blehub.local.toml or BLEHUB_NRFTEST_ROOT"
        )
    root = Path(configured.value).expanduser().resolve()
    if not (root / "pixi.toml").is_file():
        raise Nrf52840Error(
            f"configured nrftest_root is not an nrftest checkout: {root}"
        )
    return root


def _port() -> str | None:
    configured = resolve_local_value(
        "peripheral_nrf52840", "port", "BLEHUB_NRF52840_PORT", None
    )
    return configured.value


def _pid() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise Nrf52840Error(f"invalid nRF52840 fixture pid file: {PID_FILE}") from error
    return pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tail_log(lines: int = 30) -> str:
    if not LOG_FILE.is_file():
        return "<fixture log is missing>"
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def _command(nrftest_root: Path, rf_timeout: int) -> list[str]:
    pixi = shutil.which("pixi")
    if pixi is None:
        raise Nrf52840Error("pixi is required to run the external nrftest fixture")
    command = [
        pixi,
        "run",
        "python",
        "-m",
        "tools.btp_gatt_profile_probe",
        "--profile",
        str(PROFILE),
        "--rf-timeout",
        str(rf_timeout),
        "--expected-write-role",
        "read-write",
        "--expected-write-hex",
        "10",
    ]
    port = _port()
    if port is not None:
        command.extend(("--port", port))
    return command


def _wait_ready(pid: int, timeout: int, marker: str = READY_MARKER) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            raise Nrf52840Error(
                "nRF52840 fixture exited before readiness:\n" + _tail_log()
            )
        if LOG_FILE.is_file():
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            if marker in content:
                print("nrf52840_fixture=ready")
                return
        time.sleep(0.2)
    raise Nrf52840Error(
        f"nRF52840 fixture did not become ready within {timeout}s:\n" + _tail_log()
    )


def start(*, ready_timeout: int, rf_timeout: int) -> None:
    if PID_FILE.is_file():
        pid = _pid()
        if pid is not None and _alive(pid):
            _wait_ready(pid, ready_timeout)
            return
        PID_FILE.unlink(missing_ok=True)

    nrftest_root = _nrftest_root()
    if not PROFILE.is_file():
        raise Nrf52840Error(f"missing canonical nRF52840 profile: {PROFILE}")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_FILE.unlink(missing_ok=True)
    log = LOG_FILE.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _command(nrftest_root, rf_timeout),
            cwd=nrftest_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    finally:
        log.close()
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    print(f"nrf52840_fixture_pid={process.pid}")
    print(f"nrf52840_fixture_log={LOG_FILE}")
    _wait_ready(process.pid, ready_timeout)


def direct_serve(
    coordination_path: str | None = None,
    coordination_mode: str = "notification",
) -> None:
    port = _port()
    if port is None:
        raise Nrf52840Error(
            "direct nRF fixture requires peripheral_nrf52840.port or "
            + "BLEHUB_NRF52840_PORT"
        )
    serve(
        port,
        PROFILE,
        Path(coordination_path) if coordination_path else None,
        coordination_mode,
    )


def direct_start(
    coordination_path: str | None = None,
    coordination_mode: str = "notification",
) -> None:
    if PID_FILE.is_file():
        pid = _pid()
        if pid is not None and _alive(pid):
            _wait_ready(pid, DEFAULT_READY_TIMEOUT)
            return
        PID_FILE.unlink(missing_ok=True)
    if _port() is None:
        raise Nrf52840Error("nRF52840 serial port is not configured")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_FILE.unlink(missing_ok=True)
    log = LOG_FILE.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                os.sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-serve",
                *(
                    [
                        "--coordination-path",
                        coordination_path,
                        "--coordination-mode",
                        coordination_mode,
                    ]
                    if coordination_path
                    else []
                ),
            ],
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    finally:
        log.close()
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    _wait_ready(process.pid, DEFAULT_READY_TIMEOUT, "NRF52840_DIRECT_READY")


def doctor() -> None:
    port = _port()
    if port is None:
        raise Nrf52840Error(
            "nRF52840 doctor requires peripheral_nrf52840.port in ignored "
            + "blehub.local.toml or BLEHUB_NRF52840_PORT"
        )
    if not PROFILE.is_file():
        raise Nrf52840Error(f"missing canonical nRF52840 profile: {PROFILE}")
    serial = None
    try:
        serial = open_serial(port)
        result = run_profile_doctor(BtpSession(FramedTransport(serial)), PROFILE)
    except Exception as error:
        if isinstance(error, Nrf52840Error):
            raise
        raise Nrf52840Error(f"nRF52840 doctor failed: {error}") from error
    finally:
        if serial is not None:
            serial.close()
    print("nrf52840_doctor=passed")
    print(f"nrf52840_profile={result.profile_id}")
    print(f"nrf52840_supported_services=0x{result.supported_services:08x}")
    print(f"nrf52840_role_handles={result.role_handles}")


def status() -> None:
    pid = _pid()
    if pid is None:
        print("nrf52840_fixture=stopped")
        return
    if not _alive(pid):
        print("nrf52840_fixture=exited")
        print(f"nrf52840_fixture_log={LOG_FILE}")
        return
    print("nrf52840_fixture=running")
    print(f"nrf52840_fixture_pid={pid}")
    ready = LOG_FILE.is_file() and READY_MARKER in LOG_FILE.read_text(
        encoding="utf-8", errors="replace"
    )
    print(f"nrf52840_fixture_ready={str(ready).lower()}")
    print(f"nrf52840_fixture_log={LOG_FILE}")


def stop() -> None:
    pid = _pid()
    if pid is None:
        print("nrf52840_fixture=stopped")
        return
    if _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.2)
        if _alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    PID_FILE.unlink(missing_ok=True)
    print("nrf52840_fixture=stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control the external nRF52840 Peripheral fixture"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    _ = start_parser.add_argument(
        "--ready-timeout", type=int, default=DEFAULT_READY_TIMEOUT
    )
    _ = start_parser.add_argument("--rf-timeout", type=int, default=DEFAULT_RF_TIMEOUT)
    _ = subparsers.add_parser("status")
    _ = subparsers.add_parser("doctor")
    direct_start_parser = subparsers.add_parser("direct-start")
    _ = direct_start_parser.add_argument("--coordination-path")
    _ = direct_start_parser.add_argument(
        "--coordination-mode",
        choices=("notification", "indication", "passive-disconnect", "tauri", "ios-tauri"),
        default="notification",
    )
    direct_serve_parser = subparsers.add_parser("direct-serve")
    _ = direct_serve_parser.add_argument("--coordination-path")
    _ = direct_serve_parser.add_argument(
        "--coordination-mode",
        choices=("notification", "indication", "passive-disconnect", "tauri", "ios-tauri"),
        default="notification",
    )
    _ = subparsers.add_parser("ready")
    _ = subparsers.add_parser("stop")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "start":
            start(ready_timeout=args.ready_timeout, rf_timeout=args.rf_timeout)
        elif args.command == "ready":
            pid = _pid()
            if pid is None:
                raise Nrf52840Error("nRF52840 fixture is not running")
            _wait_ready(pid, DEFAULT_READY_TIMEOUT)
        elif args.command == "status":
            status()
        elif args.command == "doctor":
            doctor()
        elif args.command == "direct-start":
            direct_start(args.coordination_path, args.coordination_mode)
        elif args.command == "direct-serve":
            direct_serve(args.coordination_path, args.coordination_mode)
        else:
            stop()
    except Nrf52840Error as error:
        print(f"nRF52840 fixture error: {error}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
