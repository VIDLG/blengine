"""Failure-atomic orchestration for multiple standalone nRF52840 DK fixtures."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCAL_CONFIG_PATH = PROJECT_ROOT / "blehub.local.toml"
DEFAULT_WORK_ROOT = PROJECT_ROOT / ".work" / "peripheral" / "nrf52840" / "multi"
READY_MARKER = "NRF52840_DIRECT_READY"
DEFAULT_READY_TIMEOUT = 45
DEFAULT_STOP_TIMEOUT = 15

DEFAULT_PROFILES = {
    "dk_a": PROJECT_ROOT / "tests/conformance/scenarios/blehub-nrf-indication-v2.json",
    "dk_b": PROJECT_ROOT / "tests/conformance/scenarios/blehub-nrf-indication-v2-b.json",
}


class MultiFixtureError(RuntimeError):
    """Raised when a multi-DK fixture cannot be controlled safely."""


@dataclass(frozen=True)
class DeviceConfig:
    """Machine-local identity and profile selection for one physical DK."""

    name: str
    probe_serial: str
    port: str
    profile: Path


@dataclass
class _OwnedProcess:
    config: DeviceConfig
    log_path: Path
    process: subprocess.Popen[str]
    pid: int
    pgid: int


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MultiFixtureError(f"{field} must be a non-empty string")
    return value.strip()


def _profile_path(value: object, *, name: str) -> Path:
    if isinstance(value, Path):
        path = value.expanduser()
    else:
        configured = _non_empty_string(value, f"peripheral_nrf52840.devices.{name}.profile")
        path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _environment_override(name: str, field: str) -> str | None:
    variable = f"BLEHUB_NRF52840_{name.upper()}_{field.upper()}"
    value = os.environ.get(variable)
    return value.strip() if value is not None and value.strip() else None


def _configured_devices() -> dict[str, object]:
    if not LOCAL_CONFIG_PATH.is_file():
        raise MultiFixtureError(
            f"multi-DK configuration is missing: {LOCAL_CONFIG_PATH}; configure peripheral_nrf52840.devices in ignored blehub.local.toml"
        )
    try:
        with LOCAL_CONFIG_PATH.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise MultiFixtureError(f"cannot read {LOCAL_CONFIG_PATH}: {error}") from error
    raw_table = document.get("peripheral_nrf52840", {})
    if not isinstance(raw_table, dict):
        raise MultiFixtureError("[peripheral_nrf52840] must be a TOML table")
    table = cast(dict[str, object], raw_table)
    devices = table.get("devices")
    if not isinstance(devices, dict):
        raise MultiFixtureError(
            "[peripheral_nrf52840.devices] must define at least dk_a and dk_b"
        )
    return cast(dict[str, object], devices)


def load_devices(*, names: tuple[str, ...] = ("dk_a", "dk_b")) -> tuple[DeviceConfig, ...]:
    """Load and validate explicit physical-DK mappings from ignored local config."""

    configured = _configured_devices()
    result: list[DeviceConfig] = []
    for name in names:
        entry_value = configured.get(name)
        if not isinstance(entry_value, dict):
            raise MultiFixtureError(
                f"peripheral_nrf52840.devices.{name} is required for this gate"
            )
        entry = cast(dict[str, object], entry_value)
        probe_serial = _environment_override(name, "probe_serial") or _non_empty_string(
            entry.get("probe_serial"),
            f"peripheral_nrf52840.devices.{name}.probe_serial",
        )
        port = _environment_override(name, "port") or _non_empty_string(
            entry.get("port"), f"peripheral_nrf52840.devices.{name}.port"
        )
        profile_value = _environment_override(name, "profile") or entry.get(
            "profile", DEFAULT_PROFILES.get(name)
        )
        profile = _profile_path(profile_value, name=name)
        if not profile.is_file():
            raise MultiFixtureError(f"configured profile does not exist: {profile}")
        result.append(DeviceConfig(name, probe_serial, port, profile))

    serials = [device.probe_serial for device in result]
    ports = [device.port for device in result]
    profiles = [device.profile for device in result]
    if len(set(serials)) != len(serials):
        raise MultiFixtureError("multi-DK probe_serial values must be distinct")
    if len(set(ports)) != len(ports):
        raise MultiFixtureError("multi-DK CDC ports must be distinct")
    if len(set(profiles)) != len(profiles):
        raise MultiFixtureError("multi-DK profiles must be distinct files")
    return tuple(result)


def _validate_discovered_mappings(
    devices: tuple[DeviceConfig, ...], discovered: dict[str, str]
) -> None:
    for device in devices:
        actual = discovered.get(device.port)
        if actual is None:
            raise MultiFixtureError(
                f"{device.name} CDC port is not enumerated: {device.port} (probe_serial={device.probe_serial})"
            )
        if actual != device.probe_serial:
            raise MultiFixtureError(
                f"{device.name} CDC/probe mapping mismatch: port={device.port} reports serial={actual!r}, configured={device.probe_serial!r}"
            )


def validate_device_mappings(devices: tuple[DeviceConfig, ...]) -> None:
    """Fail closed unless each configured CDC port reports its configured USB serial."""

    try:
        from serial.tools import list_ports
    except ImportError as error:
        raise MultiFixtureError("pyserial is required to validate nRF52840 USB mappings") from error
    discovered = {
        str(port.device): str(port.serial_number or "")
        for port in list_ports.comports()
    }
    _validate_discovered_mappings(devices, discovered)


def _readiness(log_path: Path) -> bool:
    return log_path.is_file() and READY_MARKER in log_path.read_text(
        encoding="utf-8", errors="replace"
    )


def _wait_ready(owned: _OwnedProcess, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if owned.process.poll() is not None:
            tail = owned.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise MultiFixtureError(
                f"{owned.config.name} fixture exited before readiness:\n{tail}"
            )
        if _readiness(owned.log_path):
            return
        time.sleep(0.2)
    raise MultiFixtureError(
        f"{owned.config.name} fixture did not become ready within {timeout}s; see {owned.log_path}"
    )


def _stop_process(owned: _OwnedProcess, timeout: int = DEFAULT_STOP_TIMEOUT) -> None:
    if owned.process.poll() is not None:
        return
    try:
        os.killpg(owned.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while owned.process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if owned.process.poll() is None:
        try:
            os.killpg(owned.pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            _ = owned.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise MultiFixtureError(
                f"{owned.config.name} fixture process group did not exit"
            ) from error


class MultiFixtureController:
    """Own exactly one process group, log, and BTP session per configured DK."""

    def __init__(
        self,
        devices: tuple[DeviceConfig, ...] | None = None,
        *,
        work_root: Path = DEFAULT_WORK_ROOT,
        ready_timeout: int = DEFAULT_READY_TIMEOUT,
        allow_single_device: bool = False,
    ) -> None:
        self.devices: tuple[DeviceConfig, ...] = devices if devices is not None else load_devices()
        if len(self.devices) < 1:
            raise MultiFixtureError("at least one nRF52840 device is required")
        if len(self.devices) < 2 and not allow_single_device:
            raise MultiFixtureError(
                "the multi-DK gate requires at least two devices; "
                "pass allow_single_device only for a profile-aware single-DK control"
            )
        self.work_root: Path = work_root
        self.ready_timeout: int = ready_timeout
        self._owned: list[_OwnedProcess] = []

    @property
    def owned(self) -> tuple[_OwnedProcess, ...]:
        return tuple(self._owned)

    def _start_device(
        self,
        device: DeviceConfig,
        *,
        coordination_root: Path | None = None,
        coordination_mode: str = "notification",
        coordination_cycles: int = 1,
    ) -> None:
        if any(owned.config.name == device.name for owned in self._owned):
            raise MultiFixtureError(f"{device.name} fixture is already running")
        log_path = self.work_root / f"{device.name}.log"
        log = log_path.open("w", encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "tests.peripherals.nrf52840.multi_fixture",
            "serve",
            "--port",
            device.port,
            "--profile",
            str(device.profile),
        ]
        if coordination_root is not None:
            command.extend(
                (
                    "--coordination-path",
                    str(coordination_root / device.name),
                    "--coordination-mode",
                    coordination_mode,
                    "--coordination-cycles",
                    str(coordination_cycles),
                )
            )
        try:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        finally:
            log.close()
        try:
            pgid = os.getpgid(process.pid)
        except OSError as error:
            process.kill()
            raise MultiFixtureError(
                f"cannot inspect {device.name} fixture process group"
            ) from error
        owned = _OwnedProcess(device, log_path, process, process.pid, pgid)
        self._owned.append(owned)
        try:
            _wait_ready(owned, self.ready_timeout)
        except BaseException:
            self._owned.remove(owned)
            _stop_process(owned)
            raise
        print(
            f"nrf52840_multi_fixture=ready device={device.name} profile={device.profile.name} log={log_path}",
            flush=True,
        )

    def start(
        self,
        *,
        coordination_root: Path | None = None,
        coordination_mode: str = "notification",
        coordination_cycles: int = 1,
    ) -> None:
        if self._owned:
            raise MultiFixtureError("multi-DK fixture controller is already started")
        validate_device_mappings(self.devices)
        self.work_root.mkdir(parents=True, exist_ok=True)
        if coordination_root is not None:
            coordination_root.mkdir(parents=True, exist_ok=True)
        try:
            for device in self.devices:
                self._start_device(
                    device,
                    coordination_root=coordination_root,
                    coordination_mode=coordination_mode,
                    coordination_cycles=coordination_cycles,
                )
        except Exception:
            self.stop()
            raise

    def check_health(self) -> None:
        """Fail immediately when an owned fixture exits during a gate."""
        for owned in self._owned:
            returncode = owned.process.poll()
            if returncode is None:
                continue
            tail = owned.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise MultiFixtureError(
                f"{owned.config.name} fixture exited with status {returncode}:\n{tail}"
            )

    def _configured_device(self, name: str) -> DeviceConfig:
        for device in self.devices:
            if device.name == name:
                return device
        raise MultiFixtureError(f"unknown multi-DK device: {name}")

    def stop_device(self, name: str) -> None:
        """Stop exactly one owned fixture and release its controller slot."""
        owned = next((item for item in self._owned if item.config.name == name), None)
        if owned is None:
            raise MultiFixtureError(f"{name} fixture is not running")
        _stop_process(owned)
        self._owned.remove(owned)

    def restart_device(self, name: str) -> None:
        """Restart one fixture after a fault without disturbing its peers."""
        if any(owned.config.name == name for owned in self._owned):
            self.stop_device(name)
        device = self._configured_device(name)
        validate_device_mappings((device,))
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._start_device(device)

    def stop(self) -> None:
        errors: list[str] = []
        for owned in reversed(self._owned):
            try:
                _stop_process(owned)
            except MultiFixtureError as error:
                errors.append(str(error))
        self._owned.clear()
        if errors:
            raise MultiFixtureError("; ".join(errors))


def serve(port: str, profile: Path) -> None:
    """Run one direct fixture as a child owned by MultiFixtureController."""

    from .direct_fixture import serve as serve_direct

    serve_direct(port, profile)


def readiness(*, work_root: Path = DEFAULT_WORK_ROOT, ready_timeout: int = DEFAULT_READY_TIMEOUT) -> None:
    """Require every configured DK to pass BTP/profile readiness, without flashing."""

    run_root = work_root / f"readiness-{time.time_ns()}"
    controller = MultiFixtureController(
        work_root=run_root,
        ready_timeout=ready_timeout,
    )
    failed = False
    try:
        controller.start()
        print(
            f"nrf52840_multi_readiness=passed devices={len(controller.devices)}",
            flush=True,
        )
    except BaseException:
        failed = True
        print(f"nrf52840_multi_readiness=failed logs={run_root}", file=sys.stderr, flush=True)
        raise
    finally:
        controller.stop()
        if not failed:
            shutil.rmtree(run_root, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    _ = serve_parser.add_argument("--port", required=True)
    _ = serve_parser.add_argument("--profile", type=Path, required=True)
    _ = serve_parser.add_argument("--coordination-path", type=Path)
    _ = serve_parser.add_argument("--coordination-mode", default="notification")
    _ = serve_parser.add_argument("--coordination-cycles", type=int, default=1)
    readiness_parser = subparsers.add_parser("readiness")
    _ = readiness_parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    _ = readiness_parser.add_argument("--ready-timeout", type=int, default=DEFAULT_READY_TIMEOUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        command = cast(str, args.command)
        if command == "serve":
            from .direct_fixture import serve as serve_direct

            serve_direct(
                cast(str, args.port),
                cast(Path, args.profile),
                cast(Path | None, args.coordination_path),
                cast(str, args.coordination_mode),
                cast(int, args.coordination_cycles),
            )
            return 0
        if command == "readiness":
            readiness(
                work_root=cast(Path, args.work_root),
                ready_timeout=cast(int, args.ready_timeout),
            )
            return 0
        raise MultiFixtureError(f"unsupported command: {command}")
    except (MultiFixtureError, OSError, ValueError) as error:
        print(f"nRF52840 multi fixture error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
