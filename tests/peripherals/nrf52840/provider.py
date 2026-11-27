from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from tools.local_config import resolve_local_value

from .btp_session import BtpSession
from .btp_transport import FramedTransport
from .direct_fixture import require_v2_coordination, serve
from .doctor import run_profile_doctor
from .resource_lease import port_lease
from .serial_transport import open_serial

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE = (
    PROJECT_ROOT
    / "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json"
)
EXPECTED_CONTRACT = (
    PROJECT_ROOT
    / "tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json"
)
WORK_ROOT = PROJECT_ROOT / ".work" / "peripheral" / "nrf52840"
PID_FILE = WORK_ROOT / "fixture.pid"
LOG_FILE = WORK_ROOT / "fixture.log"
READY_MARKER = "NRFTEST_GATT_FIXTURE_READY "
DEFAULT_READY_TIMEOUT = 45
DEFAULT_RF_TIMEOUT = 300


class Nrf52840Error(RuntimeError):
    pass


def _require_fixture_request(request: Path | None) -> None:
    if request is None:
        from .fixture_contract import load_contract

        load_contract(EXPECTED_CONTRACT)
        raise Nrf52840Error(
            "explicit immutable --fixture-request required; v2 contract alone is not live authorization"
        )


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


def _port(explicit: str | None = None) -> str | None:
    if explicit is not None:
        if not explicit or explicit != explicit.strip():
            raise Nrf52840Error(
                "explicit nRF52840 serial port must be non-empty and trimmed"
            )
        return explicit
    configured = resolve_local_value(
        "peripheral_nrf52840", "port", "BLEHUB_NRF52840_PORT", None
    )
    return configured.value


_PROCESS_RUNNING = "running"
_PROCESS_EXITED = "exited"
_PROCESS_MISMATCH = "mismatch"
_PROCESS_UNKNOWN = "unknown"


def _process_identity() -> tuple[int, int] | None:
    if not PID_FILE.is_file():
        return None
    try:
        fields = PID_FILE.read_text(encoding="utf-8").split()
        if len(fields) not in (1, 2):
            raise ValueError("expected PID and optional PGID")
        pid = int(fields[0])
        pgid = int(fields[1]) if len(fields) == 2 else pid
        if pid <= 0 or pgid <= 0:
            raise ValueError("PID and PGID must be positive")
    except (OSError, ValueError) as error:
        raise Nrf52840Error(f"invalid nRF52840 fixture pid file: {PID_FILE}") from error
    return pid, pgid


def _pid() -> int | None:
    identity = _process_identity()
    return None if identity is None else identity[0]


def _fixture_state(pid: int, pgid: int) -> str:
    try:
        actual_pgid = os.getpgid(pid)
    except ProcessLookupError:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return _PROCESS_EXITED
        except PermissionError:
            return _PROCESS_UNKNOWN
        return _PROCESS_UNKNOWN
    except PermissionError:
        return _PROCESS_UNKNOWN
    except OSError as error:
        raise Nrf52840Error(
            f"cannot inspect nRF52840 fixture process group {pgid}"
        ) from error
    if actual_pgid != pgid:
        return _PROCESS_MISMATCH
    return _PROCESS_RUNNING


def _write_process_identity(pid: int) -> tuple[int, int]:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError as error:
        raise Nrf52840Error(
            f"nRF52840 fixture exited before its pid marker could be written: {pid}"
        ) from error
    except OSError as error:
        raise Nrf52840Error(
            f"could not inspect nRF52840 fixture process group: {pid}"
        ) from error
    if pgid != pid:
        raise Nrf52840Error(
            f"nRF52840 fixture did not create its own process group: pid={pid} pgid={pgid}"
        )
    identity = (pid, pgid)
    PID_FILE.write_text(f"{pid} {pgid}\n", encoding="utf-8")
    return identity


def _wait_for_exit(pid: int, pgid: int, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        state = _fixture_state(pid, pgid)
        if state == _PROCESS_EXITED:
            return True
        if state == _PROCESS_MISMATCH or time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


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


def _wait_ready(
    pid: int,
    timeout: int,
    marker: str = READY_MARKER,
    *,
    process: subprocess.Popen[str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Reap owned children before trusting PID liveness or an old READY marker.
        if (process is not None and process.poll() is not None) or not _alive(pid):
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
    raise Nrf52840Error(
        "transitional provider start is unavailable: use direct-start with an immutable "
        "v2 --fixture-request and live image proof; no v1 fallback"
    )


def direct_serve(
    coordination_path: str | None = None,
    coordination_mode: str = "notification",
    fixture_request: Path | None = None,
    *,
    port: str | None = None,
) -> None:
    require_v2_coordination(coordination_mode)
    _require_fixture_request(fixture_request)
    port = _port(port)
    if port is None:
        raise Nrf52840Error(
            "direct nRF fixture requires peripheral_nrf52840.port or "
            + "BLEHUB_NRF52840_PORT"
        )
    plan = None
    if fixture_request is not None:
        from .fixture_session import load_session

        plan = load_session(fixture_request)
    serve(
        port,
        plan.profile_path if plan is not None else PROFILE,
        Path(coordination_path) if coordination_path else None,
        coordination_mode,
        fixture_session=plan,
    )


def direct_start(
    coordination_path: str | None = None,
    coordination_mode: str = "notification",
    fixture_request: Path | None = None,
    *,
    port: str | None = None,
) -> None:
    require_v2_coordination(coordination_mode)
    _require_fixture_request(fixture_request)
    selected_port = _port(port)
    if coordination_mode == "ccc-disable-reject-attested":
        from .ccc_disable_reject import validate_control
        from .fixture_session import load_session

        if fixture_request is None or coordination_path is None:
            raise Nrf52840Error(
                "ccc-disable-reject-attested requires --fixture-request and --coordination-path"
            )
        validate_control(Path(coordination_path), load_session(fixture_request))
    identity = _process_identity()
    if identity is not None:
        pid, pgid = identity
        state = _fixture_state(pid, pgid)
        if state == _PROCESS_RUNNING:
            if fixture_request is not None:
                raise Nrf52840Error(
                    "cannot reuse a running nRF52840 fixture for a live-attested "
                    f"session: pid={pid} pgid={pgid}; stop it explicitly first"
                )
            _wait_ready(pid, DEFAULT_READY_TIMEOUT)
            return
        if state != _PROCESS_EXITED:
            raise Nrf52840Error(
                f"cannot safely reuse nRF52840 fixture pid={pid} pgid={pgid}: {state}; "
                f"retaining {PID_FILE}"
            )
        PID_FILE.unlink(missing_ok=True)
    if selected_port is None:
        raise Nrf52840Error("nRF52840 serial port is not configured")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_FILE.unlink(missing_ok=True)
    log = LOG_FILE.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-serve",
                "--port",
                selected_port,
                *(
                    ["--fixture-request", str(fixture_request.resolve())]
                    if fixture_request is not None
                    else []
                ),
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
    spawned_identity = _write_process_identity(process.pid)
    try:
        _wait_ready(
            process.pid,
            DEFAULT_READY_TIMEOUT,
            "NRF52840_DIRECT_READY",
            process=process,
        )
    except Exception as error:
        try:
            stop(expected_identity=spawned_identity)
        except Nrf52840Error as cleanup_error:
            raise Nrf52840Error(
                f"direct nRF fixture readiness failed and cleanup failed: {cleanup_error}"
            ) from error
        raise
    print(f"nrf52840_fixture_pid={spawned_identity[0]}")
    print(f"nrf52840_fixture_pgid={spawned_identity[1]}")


def doctor() -> None:
    port = _port()
    if port is None:
        raise Nrf52840Error(
            "nRF52840 doctor requires peripheral_nrf52840.port in ignored "
            + "blehub.local.toml or BLEHUB_NRF52840_PORT"
        )
    if not PROFILE.is_file():
        raise Nrf52840Error(f"missing canonical nRF52840 profile: {PROFILE}")
    try:
        with ExitStack() as resources:
            resources.enter_context(port_lease(port, purpose="BleHub nRF52840 doctor"))
            serial = open_serial(port)
            resources.callback(serial.close)
            session = BtpSession(FramedTransport(serial))
            resources.callback(session.close)
            result = run_profile_doctor(session, PROFILE)
    except Exception as error:
        if isinstance(error, Nrf52840Error):
            raise
        raise Nrf52840Error(f"nRF52840 doctor failed: {error}") from error
    print("nrf52840_doctor=passed")
    print(f"nrf52840_profile={result.profile_id}")
    print(f"nrf52840_supported_services=0x{result.supported_services:08x}")
    print(f"nrf52840_role_handles={result.role_handles}")


def status() -> None:
    identity = _process_identity()
    if identity is None:
        print("nrf52840_fixture=stopped")
        return
    pid, pgid = identity
    if not _alive(pid):
        print("nrf52840_fixture=exited")
        print(f"nrf52840_fixture_log={LOG_FILE}")
        return
    print("nrf52840_fixture=running")
    print(f"nrf52840_fixture_pid={pid}")
    print(f"nrf52840_fixture_pgid={pgid}")
    ready = LOG_FILE.is_file() and READY_MARKER in LOG_FILE.read_text(
        encoding="utf-8", errors="replace"
    )
    print(f"nrf52840_fixture_ready={str(ready).lower()}")
    print(f"nrf52840_fixture_log={LOG_FILE}")


def stop(expected_identity: tuple[int, int] | None = None) -> None:
    identity = _process_identity()
    if identity is None:
        if expected_identity is not None:
            raise Nrf52840Error(
                "cannot safely stop spawned nRF52840 fixture: identity marker is missing"
            )
        print("nrf52840_fixture=stopped")
        return
    if expected_identity is not None and identity != expected_identity:
        raise Nrf52840Error(
            "cannot safely stop spawned nRF52840 fixture: identity changed; "
            f"expected={expected_identity[0]} {expected_identity[1]} "
            f"found={identity[0]} {identity[1]}"
        )
    pid, pgid = identity
    state = _fixture_state(pid, pgid)
    if state == _PROCESS_EXITED:
        PID_FILE.unlink(missing_ok=True)
        print("nrf52840_fixture=stopped")
        return
    if state != _PROCESS_RUNNING:
        raise Nrf52840Error(
            f"cannot safely stop nRF52840 fixture pid={pid} pgid={pgid}: {state}; "
            f"retaining {PID_FILE}"
        )

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        if _fixture_state(pid, pgid) != _PROCESS_EXITED:
            raise Nrf52840Error(
                f"cannot signal nRF52840 fixture process group {pgid}; retaining "
                f"{PID_FILE}"
            ) from error
    except OSError as error:
        raise Nrf52840Error(
            f"could not terminate nRF52840 fixture process group {pgid}; retaining "
            f"{PID_FILE}"
        ) from error

    if _wait_for_exit(pid, pgid, 15):
        PID_FILE.unlink(missing_ok=True)
        print("nrf52840_fixture=stopped")
        return

    state = _fixture_state(pid, pgid)
    if state != _PROCESS_RUNNING:
        raise Nrf52840Error(
            f"nRF52840 fixture identity changed while stopping: pid={pid} pgid={pgid} "
            f"state={state}; retaining {PID_FILE}"
        )
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        if _fixture_state(pid, pgid) != _PROCESS_EXITED:
            raise Nrf52840Error(
                f"cannot kill nRF52840 fixture process group {pgid}; retaining "
                f"{PID_FILE}"
            ) from error
    except OSError as error:
        raise Nrf52840Error(
            f"could not kill nRF52840 fixture process group {pgid}; retaining "
            f"{PID_FILE}"
        ) from error

    if not _wait_for_exit(pid, pgid, 15):
        raise Nrf52840Error(
            f"nRF52840 fixture process group {pgid} did not exit; retaining {PID_FILE}"
        )
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
    _ = direct_start_parser.add_argument("--port")
    _ = direct_start_parser.add_argument("--coordination-path")
    _ = direct_start_parser.add_argument("--fixture-request", type=Path)
    _ = direct_start_parser.add_argument(
        "--coordination-mode",
        choices=(
            "notification",
            "indication",
            "passive-disconnect",
            "host-close",
            "recovery",
            "recovery-ios",
            "recovery-ten",
            "system-loss-attested",
            "ccc-disable-reject-attested",
        ),
        default="notification",
    )
    direct_serve_parser = subparsers.add_parser("direct-serve")
    _ = direct_serve_parser.add_argument("--port")
    _ = direct_serve_parser.add_argument("--coordination-path")
    _ = direct_serve_parser.add_argument("--fixture-request", type=Path)
    _ = direct_serve_parser.add_argument(
        "--coordination-mode",
        choices=(
            "notification",
            "indication",
            "passive-disconnect",
            "host-close",
            "recovery",
            "recovery-ios",
            "recovery-ten",
            "system-loss-attested",
            "ccc-disable-reject-attested",
        ),
        default="notification",
    )
    _ = subparsers.add_parser("ready")
    _ = subparsers.add_parser("stop")
    stop_owned_parser = subparsers.add_parser("stop-owned")
    _ = stop_owned_parser.add_argument("--pid", type=int, required=True)
    _ = stop_owned_parser.add_argument("--pgid", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "start":
            raise Nrf52840Error(
                "legacy transitional RF start is unavailable; use the attested v2 direct provider"
            )
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
            direct_start(
                args.coordination_path,
                args.coordination_mode,
                fixture_request=args.fixture_request,
                port=args.port,
            )
        elif args.command == "direct-serve":
            direct_serve(
                args.coordination_path,
                args.coordination_mode,
                fixture_request=args.fixture_request,
                port=args.port,
            )
        elif args.command == "stop-owned":
            stop(expected_identity=(args.pid, args.pgid))
        else:
            stop()
    except (ValueError, RuntimeError) as error:
        print(f"nRF52840 fixture error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
