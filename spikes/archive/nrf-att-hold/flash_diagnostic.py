"""Flash only an explicitly selected, hash-verified ATT diagnostic or restore ZIP.

This spike never resets a board or enters its bootloader.  The board must already
be enumerated as the PCA10059 USB bootloader, and both the port and bootloader
serial must be supplied explicitly.  It does not import the nrftest runtime;
managed nRF Util provenance is checked through the package-only helper in this
spike.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, cast

import package_diagnostic
from serial.tools import list_ports

BLEHUB_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIAGNOSTIC_MANIFEST = (
    BLEHUB_ROOT / ".work" / "nrf-att-hold" / "build" / "diagnostic-manifest.json"
)
STOCK_BUILD_RELATIVE_PATH = Path(".work/build/pca10059-tester")
STOCK_MANIFEST_NAME = "nrftest-build-manifest.json"
DIAGNOSTIC_PACKAGE_NAME = "nrf-att-read-hold-diagnostic-v1.zip"
STOCK_PACKAGE_NAME = "nrftest-pca10059-tester-v1.zip"
DIAGNOSTIC_PACKAGE_SHA256 = (
    "d11c9fad11623551230d5bdc79d7d50a8b8b6d73b53bf89c70756e54d32db36c"
)
STOCK_PACKAGE_SHA256 = (
    "d27fc6562febc070a928376ce765c3a07ca0c5ec6adf7aada2fa669f75f75ff7"
)
# This reviewed manifest hash is part of package_diagnostic's immutable stock
# evidence.  It prevents a changed stock manifest from selecting a different ZIP.
STOCK_MANIFEST_SHA256 = package_diagnostic.STOCK_MANIFEST_SHA256

PCA10059_BOOTLOADER_VID = 0x1915
PCA10059_BOOTLOADER_PID = 0x521F
APPLICATION_VID = 0x2FE3
APPLICATION_PID = 0x0004
TOOL_COMMAND = "nrf5sdk-tools"


class DiagnosticFlashError(RuntimeError):
    """Raised when a flash precondition or the explicit flash command fails."""


class PortInfo(Protocol):
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None


@dataclass(frozen=True)
class PackageIdentity:
    path: Path
    sha256: str


def _sha256(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DiagnosticFlashError(f"{description} must be a lowercase SHA-256 digest")
    return value


def _read_manifest(path: Path, description: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise DiagnosticFlashError(f"{description} is missing or linked: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DiagnosticFlashError(
            f"unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise DiagnosticFlashError(f"{description} root must be an object: {path}")
    return cast(dict[str, object], value)


def _safe_package_path(root: Path, value: object, description: str) -> Path:
    if not isinstance(value, str) or not value:
        raise DiagnosticFlashError(f"{description} must be a relative path")
    relative = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or relative.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or relative == PurePosixPath(".")
        or ".." in relative.parts
    ):
        raise DiagnosticFlashError(f"{description} must be a safe relative path")
    candidate = root.resolve() / Path(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise DiagnosticFlashError(f"{description} is missing or linked: {candidate}")
    path = candidate.resolve()
    if not path.is_relative_to(root.resolve()):
        raise DiagnosticFlashError(f"{description} escapes {root}")
    return path


def _hash_file(path: Path) -> str:
    try:
        return package_diagnostic.sha256_file(path)
    except package_diagnostic.DiagnosticPackageError as error:
        raise DiagnosticFlashError(str(error)) from error


def _validate_diagnostic_package(manifest_path: Path) -> PackageIdentity:
    manifest = _read_manifest(manifest_path, "diagnostic manifest")
    if manifest.get("schema_version") != 1 or isinstance(
        manifest.get("schema_version"), bool
    ):
        raise DiagnosticFlashError(
            "diagnostic manifest schema_version must be integer 1"
        )
    if manifest.get("kind") != package_diagnostic.DIAGNOSTIC_KIND:
        raise DiagnosticFlashError(
            "diagnostic manifest kind is not the ATT read-hold diagnostic"
        )
    if manifest.get("board") != package_diagnostic.BOARD:
        raise DiagnosticFlashError(
            "diagnostic manifest board is not nrf52840dongle/nrf52840"
        )

    package_value = manifest.get("dfu_package")
    if not isinstance(package_value, dict):
        raise DiagnosticFlashError("diagnostic manifest has no validated DFU package")
    package = cast(dict[str, object], package_value)
    path = _safe_package_path(
        manifest_path.parent, package.get("path"), "diagnostic package path"
    )
    if path.name != DIAGNOSTIC_PACKAGE_NAME:
        raise DiagnosticFlashError(
            f"diagnostic package must be named {DIAGNOSTIC_PACKAGE_NAME}"
        )
    recorded_sha = _sha256(package.get("sha256"), "diagnostic package manifest sha256")
    if recorded_sha != DIAGNOSTIC_PACKAGE_SHA256:
        raise DiagnosticFlashError(
            f"diagnostic package manifest SHA changed: expected {DIAGNOSTIC_PACKAGE_SHA256}, "
            f"found {recorded_sha}"
        )
    actual_sha = _hash_file(path)
    if actual_sha != DIAGNOSTIC_PACKAGE_SHA256:
        raise DiagnosticFlashError(
            f"diagnostic package changed: expected {DIAGNOSTIC_PACKAGE_SHA256}, found {actual_sha}"
        )
    return PackageIdentity(path, actual_sha)


def _stock_manifest_path(nrftest_root: Path) -> Path:
    return nrftest_root / STOCK_BUILD_RELATIVE_PATH / STOCK_MANIFEST_NAME


def _validate_stock_package(nrftest_root: Path) -> PackageIdentity:
    manifest_path = _stock_manifest_path(nrftest_root)
    try:
        _manifest, path, actual_sha = package_diagnostic._validate_stock(
            manifest_path, STOCK_MANIFEST_SHA256, STOCK_PACKAGE_SHA256
        )
    except package_diagnostic.DiagnosticPackageError as error:
        raise DiagnosticFlashError(str(error)) from error
    if path.name != STOCK_PACKAGE_NAME:
        raise DiagnosticFlashError(f"stock package must be named {STOCK_PACKAGE_NAME}")
    return PackageIdentity(path, actual_sha)


def _select_package(
    mode: str, diagnostic_manifest: Path, nrftest_root: Path
) -> PackageIdentity:
    stock = _validate_stock_package(nrftest_root)
    if mode == "restore":
        return stock
    if mode != "diagnostic":
        raise DiagnosticFlashError(f"unsupported flash mode: {mode}")
    # Diagnostic flashing is never allowed to bypass the known-good restore path.
    return _validate_diagnostic_package(diagnostic_manifest)


def _port_identity(port: PortInfo) -> tuple[str, int | None, int | None, str | None]:
    return (port.device, port.vid, port.pid, port.serial_number)


def _find_explicit_port(port_name: str, ports: Sequence[PortInfo]) -> PortInfo:
    if not port_name.strip():
        raise DiagnosticFlashError("--port must be explicitly provided")
    matches = [port for port in ports if port.device.casefold() == port_name.casefold()]
    if len(matches) != 1:
        available = ", ".join(port.device for port in ports) or "<none>"
        raise DiagnosticFlashError(
            f"explicit port {port_name!r} is not uniquely present; available ports: {available}"
        )
    return matches[0]


def _validate_bootloader_port(port: PortInfo, bootloader_serial: str) -> PortInfo:
    if not bootloader_serial.strip():
        raise DiagnosticFlashError("--bootloader-serial must be explicitly provided")
    if port.vid == APPLICATION_VID and port.pid == APPLICATION_PID:
        raise DiagnosticFlashError(
            "application port 2FE3:0004 is not a bootloader port"
        )
    if port.vid != PCA10059_BOOTLOADER_VID or port.pid != PCA10059_BOOTLOADER_PID:
        observed = (
            f"{port.vid:04X}:{port.pid:04X}"
            if port.vid is not None and port.pid is not None
            else "<missing>"
        )
        raise DiagnosticFlashError(
            f"explicit port is not the PCA10059 bootloader: expected 1915:521F, found {observed}"
        )
    if not isinstance(port.serial_number, str) or not port.serial_number.strip():
        raise DiagnosticFlashError(
            "explicit PCA10059 bootloader port has no USB serial identity"
        )
    if port.serial_number != bootloader_serial:
        raise DiagnosticFlashError(
            f"PCA10059 bootloader serial mismatch: expected {bootloader_serial!r}, "
            f"found {port.serial_number!r}"
        )
    return port


def _select_bootloader_port(
    port_name: str, bootloader_serial: str, ports: Sequence[PortInfo]
) -> PortInfo:
    return _validate_bootloader_port(
        _find_explicit_port(port_name, ports), bootloader_serial
    )


def _revalidate_bootloader_port(
    selected: PortInfo,
    port_name: str,
    bootloader_serial: str,
    ports: Sequence[PortInfo],
) -> PortInfo:
    current = _find_explicit_port(port_name, ports)
    if _port_identity(current) != _port_identity(selected):
        raise DiagnosticFlashError(
            "PCA10059 bootloader identity changed before nRF Util launch: "
            f"expected {_port_identity(selected)!r}, found {_port_identity(current)!r}"
        )
    return _validate_bootloader_port(current, bootloader_serial)


def _tool_environment(executable: Path) -> dict[str, str]:
    install_root = executable.parent.parent
    environment = dict(os.environ)
    environment["NRFUTIL_HOME"] = str(install_root)
    environment["PATH"] = (
        str(install_root / "bin") + os.pathsep + environment.get("PATH", "")
    )
    return environment


def flash(
    *,
    mode: str,
    port_name: str,
    bootloader_serial: str,
    confirmed_sha256: str,
    diagnostic_manifest: Path,
    nrftest_root: Path,
) -> PackageIdentity:
    """Validate every input, then invoke exactly one explicit DFU command."""
    if mode not in {"diagnostic", "restore"}:
        raise DiagnosticFlashError("--mode must be diagnostic or restore")
    if not port_name.strip():
        raise DiagnosticFlashError("--port must be explicitly provided")
    if not bootloader_serial.strip():
        raise DiagnosticFlashError("--bootloader-serial must be explicitly provided")
    if not confirmed_sha256 or any(
        character not in "0123456789abcdef" for character in confirmed_sha256
    ):
        raise DiagnosticFlashError(
            "--confirm-sha256 must be a lowercase SHA-256 digest"
        )

    try:
        root = nrftest_root.expanduser().resolve(strict=True)
        raw_manifest = diagnostic_manifest.expanduser()
        if mode == "diagnostic" and raw_manifest.is_symlink():
            raise DiagnosticFlashError(f"diagnostic manifest is linked: {raw_manifest}")
        manifest_path = (
            raw_manifest.resolve(strict=True) if mode == "diagnostic" else raw_manifest
        )
    except OSError as error:
        raise DiagnosticFlashError(
            f"required flash input is missing: {error}"
        ) from error
    if not root.is_dir():
        raise DiagnosticFlashError(f"nrftest root is not a directory: {root}")

    # Select and hash-check the package before touching the USB enumeration.
    package = _select_package(mode, manifest_path, root)
    confirmed = _sha256(confirmed_sha256, "--confirm-sha256")
    if confirmed != package.sha256:
        raise DiagnosticFlashError(
            f"package confirmation mismatch: expected exact SHA-256 {package.sha256}"
        )

    try:
        tool = package_diagnostic.verify_tool(root, None)
    except package_diagnostic.DiagnosticPackageError as error:
        raise DiagnosticFlashError(str(error)) from error
    if tool.get("command") != TOOL_COMMAND:
        raise DiagnosticFlashError(f"managed tool command must be {TOOL_COMMAND}")
    executable_value = tool.get("executable")
    if not isinstance(executable_value, str) or not executable_value:
        raise DiagnosticFlashError("managed tool provenance has no executable")
    executable = Path(executable_value).expanduser()

    selected = _select_bootloader_port(
        port_name,
        bootloader_serial,
        cast(list[PortInfo], list(list_ports.comports())),
    )
    verified = _revalidate_bootloader_port(
        selected,
        port_name,
        bootloader_serial,
        cast(list[PortInfo], list(list_ports.comports())),
    )

    command = [
        str(executable),
        TOOL_COMMAND,
        "dfu",
        "usb-serial",
        "-pkg",
        str(package.path),
        "-p",
        verified.device,
    ]
    print(f"Selected {mode} package: {package.path}")
    print(f"Package SHA-256: {package.sha256}")
    print(
        f"Verified bootloader: {verified.device} "
        f"{verified.vid:04X}:{verified.pid:04X} {verified.serial_number}"
    )
    print(f"+ {subprocess.list2cmdline(command)}")
    try:
        subprocess.run(
            command, env=_tool_environment(executable), check=True, timeout=600
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise DiagnosticFlashError(f"firmware flash failed: {error}") from error
    return package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("diagnostic", "restore"), required=True)
    parser.add_argument(
        "--port", required=True, help="exact current bootloader serial port"
    )
    parser.add_argument(
        "--bootloader-serial", required=True, help="exact bootloader USB serial"
    )
    parser.add_argument(
        "--confirm-sha256", required=True, help="exact selected ZIP SHA-256"
    )
    parser.add_argument(
        "--diagnostic-manifest", type=Path, default=DEFAULT_DIAGNOSTIC_MANIFEST
    )
    parser.add_argument("--nrftest-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        package = flash(
            mode=arguments.mode,
            port_name=arguments.port,
            bootloader_serial=arguments.bootloader_serial,
            confirmed_sha256=arguments.confirm_sha256,
            diagnostic_manifest=arguments.diagnostic_manifest,
            nrftest_root=arguments.nrftest_root,
        )
    except (DiagnosticFlashError, OSError, UnicodeError) as error:
        print(f"diagnostic flash error: {error}", file=sys.stderr)
        return 2
    print(f"Firmware flash command succeeded for {package.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
