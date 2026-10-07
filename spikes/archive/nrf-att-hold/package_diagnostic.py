"""Safely package the isolated nRF ATT read-hold diagnostic image.

This is deliberately package-only.  It validates an already-built diagnostic
manifest, compares it with the immutable stock nrftest build, invokes only the
pinned nRF5 SDK packaging command, and records package provenance.  It never
opens a serial port, enumerates USB devices, or flashes hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

import tomllib

BLEHUB_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIAGNOSTIC_MANIFEST = (
    BLEHUB_ROOT / ".work" / "nrf-att-hold" / "build" / "diagnostic-manifest.json"
)
BOARD = "nrf52840dongle/nrf52840"
DIAGNOSTIC_KIND = "nrf-att-read-hold-diagnostic"
PINNED_ZEPHYR_HEAD = "b5f309bc2da57b49012648c9432d0d4cb5ee1cc8"
PACKAGE_CONFIG_RELATIVE_PATH = Path("firmware/package.toml")
FIRMWARE_TOOLS_LOCK_NAME = "firmware-tools.lock.toml"
STOCK_BUILD_RELATIVE_PATH = Path(".work/build/pca10059-tester")
STOCK_MANIFEST_NAME = "nrftest-build-manifest.json"
STOCK_PACKAGE_SHA256 = (
    "d27fc6562febc070a928376ce765c3a07ca0c5ec6adf7aada2fa669f75f75ff7"
)
# This is the reviewed stock manifest accompanying STOCK_PACKAGE_SHA256.  The
# generated_at_utc field is intentionally covered: changing stock evidence must
# be an explicit, reviewed baseline change rather than silently accepted.
STOCK_MANIFEST_SHA256 = (
    "5f6d53e5c575002feaa1060a5f35e496dbc808c71dde78b56cb5ea4d53bb79c4"
)
DIAGNOSTIC_PACKAGE_NAME = "nrf-att-read-hold-diagnostic-v1.zip"
APPLICATION_START = 0x1000
BOOTLOADER_START = 0xE0000
NORMALIZED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXPECTED_PACKAGE_CONFIG = {
    "schema_version": 1,
    "format": "nrf5-sdk-secure-dfu",
    "hardware_version": 52,
    "softdevice_requirement": "0x00",
    "application_version": 1,
    "signed": False,
}
PLATFORM_TARGETS = {
    ("Windows", "amd64"): "x86_64-pc-windows-msvc",
    ("Windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "aarch64"): "aarch64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "amd64"): "x86_64-unknown-linux-gnu",
}


class DiagnosticPackageError(RuntimeError):
    """Raised when packaging inputs, provenance, or output are unsafe."""


@dataclass(frozen=True)
class PackageConfig:
    format: str
    hardware_version: int
    softdevice_requirement: str
    application_version: int
    signed: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise DiagnosticPackageError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _read_json(path: Path, description: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DiagnosticPackageError(
            f"unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise DiagnosticPackageError(f"{description} root must be an object: {path}")
    return cast(dict[str, object], value)


def _require_sha(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DiagnosticPackageError(
            f"{description} must be a lowercase SHA-256 digest"
        )
    return value


def _safe_relative_path(root: Path, value: object, description: str) -> Path:
    if not isinstance(value, str) or not value:
        raise DiagnosticPackageError(f"{description} must be a relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DiagnosticPackageError(f"{description} must stay inside {root}")
    path = root.joinpath(*relative.parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise DiagnosticPackageError(
            f"{description} escapes or is missing: {path}"
        ) from error
    if path.is_symlink():
        raise DiagnosticPackageError(f"{description} must not be a symlink: {path}")
    return path


def _recorded_input(
    manifest: Mapping[str, object],
    name: str,
    expected_path: Path,
) -> str:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise DiagnosticPackageError("diagnostic manifest inputs must be an object")
    entry = inputs.get(name)
    if not isinstance(entry, dict):
        raise DiagnosticPackageError(
            f"diagnostic manifest input {name} must be an object"
        )
    recorded_path = entry.get("path")
    if not isinstance(recorded_path, str):
        raise DiagnosticPackageError(
            f"diagnostic manifest input {name}.path must be a string"
        )
    path = Path(recorded_path).expanduser()
    if not path.is_absolute():
        path = (BLEHUB_ROOT / path).resolve()
    else:
        path = path.resolve()
    if path != expected_path.resolve():
        raise DiagnosticPackageError(
            f"diagnostic input {name} points to {path}, expected {expected_path.resolve()}"
        )
    expected_sha = _require_sha(entry.get("sha256"), f"diagnostic input {name}.sha256")
    if not path.is_file() or path.is_symlink():
        raise DiagnosticPackageError(
            f"diagnostic input {name} is missing or linked: {path}"
        )
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise DiagnosticPackageError(
            f"diagnostic input {name} changed: expected {expected_sha}, found {actual_sha}"
        )
    return actual_sha


def _artifact_sha(manifest: Mapping[str, object], relative_path: str) -> str:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise DiagnosticPackageError("diagnostic manifest artifacts must be an array")
    matches = [
        value
        for value in artifacts
        if isinstance(value, dict) and value.get("path") == relative_path
    ]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise DiagnosticPackageError(
            f"diagnostic manifest must contain exactly one {relative_path}"
        )
    return _require_sha(matches[0].get("sha256"), f"artifact {relative_path}.sha256")


def _parse_hex(path: Path) -> list[tuple[int, int]]:
    """Parse data records and return coalesced absolute address segments."""
    data: dict[int, int] = {}
    base = 0
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise DiagnosticPackageError(
            f"cannot read Intel HEX {path}: {error}"
        ) from error
    saw_eof = False
    for line_number, line in enumerate(lines, 1):
        if not line.startswith(":") or len(line) < 11:
            raise DiagnosticPackageError(
                f"invalid Intel HEX record at {path}:{line_number}"
            )
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as error:
            raise DiagnosticPackageError(
                f"invalid Intel HEX characters at {path}:{line_number}"
            ) from error
        length = record[0]
        if len(record) != length + 5 or sum(record) & 0xFF:
            raise DiagnosticPackageError(
                f"invalid Intel HEX checksum at {path}:{line_number}"
            )
        address = (record[1] << 8) | record[2]
        record_type = record[3]
        payload = record[4:-1]
        if record_type == 0x00:
            absolute = base + address
            for offset, byte in enumerate(payload):
                location = absolute + offset
                previous = data.get(location)
                if previous is not None and previous != byte:
                    raise DiagnosticPackageError(
                        f"conflicting Intel HEX data at 0x{location:x}"
                    )
                data[location] = byte
        elif record_type == 0x01:
            if payload or address:
                raise DiagnosticPackageError(
                    f"malformed Intel HEX EOF at {path}:{line_number}"
                )
            saw_eof = True
            break
        elif record_type == 0x02:
            if length != 2:
                raise DiagnosticPackageError(
                    f"malformed Intel HEX segment base at {path}:{line_number}"
                )
            base = int.from_bytes(payload, "big") << 4
        elif record_type == 0x04:
            if length != 2:
                raise DiagnosticPackageError(
                    f"malformed Intel HEX linear base at {path}:{line_number}"
                )
            base = int.from_bytes(payload, "big") << 16
        elif record_type in (0x03, 0x05):
            if length != 4:
                raise DiagnosticPackageError(
                    f"malformed Intel HEX metadata at {path}:{line_number}"
                )
        else:
            raise DiagnosticPackageError(
                f"unsupported Intel HEX record type 0x{record_type:02x} at {path}:{line_number}"
            )
    if not saw_eof or not data:
        raise DiagnosticPackageError(
            f"Intel HEX has no complete application image: {path}"
        )
    addresses = sorted(data)
    segments: list[tuple[int, int]] = []
    start = previous = addresses[0]
    for address in addresses[1:]:
        if address != previous + 1:
            segments.append((start, previous + 1))
            start = address
        previous = address
    segments.append((start, previous + 1))
    return segments


def validate_flash_segments(
    segments: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not segments:
        raise DiagnosticPackageError("diagnostic HEX contains no data")
    normalized = sorted((int(start), int(end)) for start, end in segments)
    previous_end = APPLICATION_START
    for start, end in normalized:
        if start < APPLICATION_START or end > BOOTLOADER_START or start >= end:
            raise DiagnosticPackageError(
                f"diagnostic HEX segment outside application flash: 0x{start:x}-0x{end:x}"
            )
        if start < previous_end:
            raise DiagnosticPackageError("diagnostic HEX segments overlap")
        previous_end = end
    return normalized


def _validate_manifest(
    manifest_path: Path, nrftest_root: Path
) -> tuple[dict[str, object], Path, str, list[tuple[int, int]]]:
    manifest = _read_json(manifest_path, "diagnostic manifest")
    if manifest.get("schema_version") != 1 or isinstance(
        manifest.get("schema_version"), bool
    ):
        raise DiagnosticPackageError(
            "diagnostic manifest schema_version must be integer 1"
        )
    if manifest.get("kind") != DIAGNOSTIC_KIND:
        raise DiagnosticPackageError(
            f"diagnostic manifest kind must be {DIAGNOSTIC_KIND}"
        )
    if manifest.get("board") != BOARD:
        raise DiagnosticPackageError(f"diagnostic manifest board must be {BOARD}")
    if manifest.get("zephyr_head") != PINNED_ZEPHYR_HEAD:
        raise DiagnosticPackageError(
            f"diagnostic Zephyr HEAD is not pinned {PINNED_ZEPHYR_HEAD}"
        )
    if manifest.get("nrftest_root") != str(nrftest_root.resolve()):
        raise DiagnosticPackageError(
            f"diagnostic nrftest_root must be {nrftest_root.resolve()}"
        )
    if manifest.get("application") != "tests/bluetooth/tester":
        raise DiagnosticPackageError(
            "diagnostic manifest application is not the Tester"
        )
    if manifest.get("dfu_package") is not None:
        raise DiagnosticPackageError(
            "diagnostic manifest already contains package evidence"
        )

    nrftest_root = nrftest_root.resolve(strict=True)
    manifest_root = manifest_path.parent.resolve(strict=True)
    patch_path = (
        BLEHUB_ROOT
        / "spikes"
        / "nrf-att-hold"
        / "firmware"
        / "0001-bttester-gatt-read-hold.patch"
    )
    expected_inputs = {
        "canonical_config": nrftest_root / "firmware" / "app" / "pca10059.conf",
        "overlay": nrftest_root / "firmware" / "app" / "pca10059.overlay",
        "patch": patch_path,
        "diagnostic_config": manifest_root.parent / "diagnostic.conf",
    }
    for name, expected_path in expected_inputs.items():
        _recorded_input(manifest, name, expected_path)

    hex_sha = _artifact_sha(manifest, "zephyr/zephyr.hex")
    hex_path = _safe_relative_path(manifest_root, "zephyr/zephyr.hex", "diagnostic HEX")
    if sha256_file(hex_path) != hex_sha:
        raise DiagnosticPackageError(
            f"diagnostic HEX changed: expected {hex_sha}, found {sha256_file(hex_path)}"
        )

    segments_value = manifest.get("flash_segments")
    if not isinstance(segments_value, list):
        raise DiagnosticPackageError(
            "diagnostic manifest flash_segments must be an array"
        )
    declared: list[tuple[int, int]] = []
    for value in segments_value:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("start"), str)
            or not isinstance(value.get("end_exclusive"), str)
        ):
            raise DiagnosticPackageError(
                "diagnostic flash segment entries are malformed"
            )
        try:
            declared.append((int(value["start"], 16), int(value["end_exclusive"], 16)))
        except ValueError as error:
            raise DiagnosticPackageError(
                "diagnostic flash segment addresses must be hexadecimal"
            ) from error
    declared = validate_flash_segments(declared)
    actual = validate_flash_segments(_parse_hex(hex_path))
    if declared != actual:
        raise DiagnosticPackageError(
            f"diagnostic flash segments do not match HEX: declared {declared}, actual {actual}"
        )
    return manifest, hex_path, hex_sha, actual


def _load_package_config(nrftest_root: Path) -> PackageConfig:
    path = nrftest_root / PACKAGE_CONFIG_RELATIVE_PATH
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DiagnosticPackageError(
            f"unable to read package config {path}: {error}"
        ) from error
    if (
        not isinstance(document.get("schema_version"), int)
        or isinstance(document.get("schema_version"), bool)
        or document.get("schema_version") != 1
        or document.get("signed") is not False
        or any(
            document.get(key) != value
            for key, value in EXPECTED_PACKAGE_CONFIG.items()
            if key not in {"schema_version", "signed"}
        )
    ):
        raise DiagnosticPackageError(
            "nrftest firmware/package.toml does not match pinned diagnostic package parameters"
        )
    return PackageConfig(
        format=cast(str, document["format"]),
        hardware_version=cast(int, document["hardware_version"]),
        softdevice_requirement=cast(str, document["softdevice_requirement"]),
        application_version=cast(int, document["application_version"]),
        signed=cast(bool, document["signed"]),
    )


def _platform_target() -> str:
    try:
        return PLATFORM_TARGETS[(platform.system(), platform.machine().lower())]
    except KeyError as error:
        raise DiagnosticPackageError(
            f"unsupported managed nRF Util host: {platform.system()} {platform.machine()}"
        ) from error


def _tool_environment(install_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["NRFUTIL_HOME"] = str(install_root)
    environment["PATH"] = (
        str(install_root / "bin") + os.pathsep + environment.get("PATH", "")
    )
    return environment


def _run_capture(command: Sequence[str], *, environment: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            env=dict(environment),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise DiagnosticPackageError(
            f"managed nRF Util identity command failed: {subprocess.list2cmdline(list(command))}: {error}"
        ) from error
    return ((result.stdout or "") + (result.stderr or "")).strip()


def _configured_nrfutil(nrftest_root: Path) -> Path | None:
    config_path = nrftest_root / "nrftest.local.toml"
    if not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DiagnosticPackageError(
            f"unable to read nrftest local config {config_path}: {error}"
        ) from error
    tools = document.get("tools")
    if not isinstance(tools, dict) or not isinstance(tools.get("programmer"), str):
        return None
    path = Path(cast(str, tools["programmer"])).expanduser()
    return path if path.is_absolute() else nrftest_root / path


def verify_tool(nrftest_root: Path, explicit_nrfutil: Path | None) -> dict[str, object]:
    """Verify lockfile metadata, installed command metadata, and executable identity."""
    lock_path = nrftest_root / FIRMWARE_TOOLS_LOCK_NAME
    try:
        with lock_path.open("rb") as stream:
            lock = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise DiagnosticPackageError(
            f"unable to read firmware tool lock {lock_path}: {error}"
        ) from error
    nrfutil_lock = lock.get("nrfutil")
    platforms = lock.get("platforms")
    if not isinstance(nrfutil_lock, dict) or not isinstance(platforms, dict):
        raise DiagnosticPackageError("firmware tool lock is missing nrfutil metadata")
    command = nrfutil_lock.get("command")
    target = _platform_target()
    platform_lock = platforms.get(
        next(
            (
                key
                for key, value in platforms.items()
                if isinstance(value, dict) and value.get("target") == target
            ),
            "",
        )
    )
    if not isinstance(command, dict) or not isinstance(platform_lock, dict):
        raise DiagnosticPackageError(
            f"firmware tool lock has no {target} nrf5sdk-tools entry"
        )
    command_name = command.get("name")
    command_version = command.get("version")
    command_commit = command.get("commit")
    inner_version = command.get("inner_version")
    cli_version = nrfutil_lock.get("version")
    cli_commit = nrfutil_lock.get("commit")
    if not all(
        isinstance(value, str) and value
        for value in (
            command_name,
            command_version,
            command_commit,
            inner_version,
            cli_version,
            cli_commit,
        )
    ):
        raise DiagnosticPackageError(
            "firmware tool lock contains incomplete version metadata"
        )
    if command_name != "nrf5sdk-tools":
        raise DiagnosticPackageError("firmware tool lock command must be nrf5sdk-tools")

    nrfutil = explicit_nrfutil or _configured_nrfutil(nrftest_root)
    if nrfutil is None:
        raise DiagnosticPackageError(
            "provide --nrfutil or configure [tools].programmer in nrftest.local.toml"
        )
    nrfutil = nrfutil.expanduser()
    if nrfutil.is_symlink():
        raise DiagnosticPackageError(
            f"managed nRF Util executable is linked: {nrfutil}"
        )
    try:
        nrfutil = nrfutil.resolve(strict=True)
    except OSError as error:
        raise DiagnosticPackageError(
            f"managed nRF Util executable is missing: {nrfutil}"
        ) from error
    if not nrfutil.is_file():
        raise DiagnosticPackageError(
            f"managed nRF Util executable is missing: {nrfutil}"
        )
    if nrfutil.name != "nrfutil" or nrfutil.parent.name != "bin":
        raise DiagnosticPackageError(
            f"--nrfutil must select a managed bin/nrfutil: {nrfutil}"
        )
    install_root = nrfutil.parent.parent
    environment = _tool_environment(install_root)
    version_output = _run_capture([str(nrfutil), "--version"], environment=environment)
    if (
        f"nrfutil {cli_version} " not in version_output
        or str(cli_commit) not in version_output
    ):
        raise DiagnosticPackageError(
            f"managed nRF Util identity mismatch:\n{version_output}"
        )

    manifest_path = (
        install_root
        / "installed"
        / f"nrfutil-{command_name}-{target}"
        / "manifest.json"
    )
    installed = _read_json(manifest_path, "installed nrf5sdk-tools manifest")
    expected_installed = {
        "name": f"nrfutil-{command_name}",
        "version": command_version,
        "platform": target,
        "build": command_commit,
    }
    mismatches = [
        f"{key}: expected {expected}, found {installed.get(key)}"
        for key, expected in expected_installed.items()
        if installed.get(key) != expected
    ]
    if mismatches:
        raise DiagnosticPackageError(
            "installed nrf5sdk-tools identity mismatch: " + "; ".join(mismatches)
        )

    command_executable = nrfutil.parent / f"nrfutil-{command_name}"
    if not command_executable.is_file() or command_executable.is_symlink():
        raise DiagnosticPackageError(
            f"managed command executable is missing or linked: {command_executable}"
        )
    archive_hashes: dict[str, str] = {}
    local_config = nrftest_root / "nrftest.local.toml"
    downloads_root: Path | None = None
    if local_config.is_file():
        try:
            with local_config.open("rb") as stream:
                local = cast(dict[str, object], tomllib.load(stream))
            paths = local.get("paths")
            if isinstance(paths, dict) and isinstance(paths.get("downloads"), str):
                downloads_root = (
                    nrftest_root / cast(str, paths["downloads"])
                ).resolve()
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise DiagnosticPackageError(
                f"unable to read nrftest local config {local_config}: {error}"
            ) from error
    for key in ("cli_archive", "command_archive"):
        archive_name = platform_lock.get(key)
        archive_sha = platform_lock.get(key.removesuffix("_archive") + "_sha256")
        if (
            isinstance(archive_name, str)
            and isinstance(archive_sha, str)
            and downloads_root is not None
        ):
            archive = downloads_root / archive_name
            if archive.is_file() and sha256_file(archive) != archive_sha:
                raise DiagnosticPackageError(
                    f"managed firmware tool archive changed: {archive}"
                )
            if archive.is_file():
                archive_hashes[key] = sha256_file(archive)

    return {
        "executable": str(nrfutil),
        "executable_sha256": sha256_file(nrfutil),
        "command_executable": str(command_executable),
        "command_executable_sha256": sha256_file(command_executable),
        "nrfutil_version": cli_version,
        "nrfutil_commit": cli_commit,
        "command": command_name,
        "command_version": command_version,
        "command_commit": command_commit,
        "inner_version": inner_version,
        "installed_manifest": str(manifest_path),
        "installed_manifest_sha256": sha256_file(manifest_path),
        "archive_sha256": archive_hashes,
    }


def _invoke_tool(
    nrfutil: Path,
    command_name: str,
    arguments: Sequence[str],
    install_root: Path,
    timeout: int,
) -> None:
    command = [str(nrfutil), command_name, *arguments]
    try:
        subprocess.run(
            command,
            env=_tool_environment(install_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise DiagnosticPackageError(
            f"nrf5sdk-tools {arguments[0]} failed without touching hardware: {error}"
        ) from error


def normalize_package_zip(path: Path) -> list[str]:
    normalized = path.with_name(path.name + ".normalized")
    if normalized.exists() or normalized.is_symlink():
        raise DiagnosticPackageError(
            f"normalization output already exists: {normalized}"
        )
    try:
        with zipfile.ZipFile(path) as source:
            names = source.namelist()
            if len(names) != len(set(names)):
                raise DiagnosticPackageError(
                    "generated DFU ZIP contains duplicate members"
                )
            for name in names:
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts or "\\" in name:
                    raise DiagnosticPackageError(f"unsafe DFU ZIP member: {name}")
            with zipfile.ZipFile(
                normalized, "w", compression=zipfile.ZIP_STORED
            ) as destination:
                for name in sorted(names):
                    info = zipfile.ZipInfo(name, NORMALIZED_ZIP_TIMESTAMP)
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.external_attr = 0o600 << 16
                    destination.writestr(info, source.read(name))
    except (OSError, zipfile.BadZipFile) as error:
        if normalized.exists():
            normalized.unlink()
        raise DiagnosticPackageError(
            f"unable to normalize generated DFU ZIP {path}: {error}"
        ) from error
    try:
        normalized.replace(path)
    except OSError as error:
        normalized.unlink(missing_ok=True)
        raise DiagnosticPackageError(
            f"unable to publish normalized DFU ZIP {path}: {error}"
        ) from error
    return sorted(names)


def _inspect_package(path: Path) -> tuple[list[str], dict[str, object]]:
    try:
        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            if "manifest.json" not in names:
                raise DiagnosticPackageError("DFU ZIP does not contain manifest.json")
            embedded = json.loads(package.read("manifest.json"))
    except (OSError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as error:
        raise DiagnosticPackageError(
            f"unable to inspect normalized DFU ZIP {path}: {error}"
        ) from error
    if not isinstance(embedded, dict) or not isinstance(embedded.get("manifest"), dict):
        raise DiagnosticPackageError(
            "DFU ZIP manifest does not describe an application image"
        )
    application = cast(dict[str, object], embedded["manifest"]).get("application")
    if not isinstance(application, dict):
        raise DiagnosticPackageError("DFU ZIP manifest application entry is malformed")
    return sorted(names), cast(dict[str, object], embedded)


def _validate_stock(
    stock_manifest_path: Path,
    expected_manifest_sha256: str,
    expected_package_sha256: str,
) -> tuple[dict[str, object], Path, str]:
    if not stock_manifest_path.is_file() or stock_manifest_path.is_symlink():
        raise DiagnosticPackageError(
            f"stock manifest is missing or linked: {stock_manifest_path}"
        )
    actual_manifest_sha = sha256_file(stock_manifest_path)
    if actual_manifest_sha != expected_manifest_sha256:
        raise DiagnosticPackageError(
            f"stock manifest changed: expected {expected_manifest_sha256}, found {actual_manifest_sha}"
        )
    manifest = _read_json(stock_manifest_path, "stock manifest")
    if (
        not isinstance(manifest.get("schema_version"), int)
        or isinstance(manifest.get("schema_version"), bool)
        or manifest.get("schema_version") != 1
        or manifest.get("board") != BOARD
    ):
        raise DiagnosticPackageError(
            "stock manifest schema or board identity does not match"
        )
    package_value = manifest.get("dfu_package")
    if not isinstance(package_value, dict):
        raise DiagnosticPackageError("stock manifest does not contain package evidence")
    embedded_sha = _require_sha(
        package_value.get("sha256"), "stock package manifest sha256"
    )
    if embedded_sha != expected_package_sha256:
        raise DiagnosticPackageError(
            f"stock package manifest SHA changed: expected {expected_package_sha256}, found {embedded_sha}"
        )
    package_path = _safe_relative_path(
        stock_manifest_path.parent, package_value.get("path"), "stock package path"
    )
    actual_package_sha = sha256_file(package_path)
    if actual_package_sha != expected_package_sha256:
        raise DiagnosticPackageError(
            f"stock package changed: expected {expected_package_sha256}, found {actual_package_sha}"
        )
    return manifest, package_path, actual_package_sha


def package(
    diagnostic_manifest: Path,
    nrftest_root: Path,
    *,
    stock_manifest: Path | None = None,
    nrfutil: Path | None = None,
    output: Path | None = None,
    expected_stock_manifest_sha256: str = STOCK_MANIFEST_SHA256,
    expected_stock_package_sha256: str = STOCK_PACKAGE_SHA256,
) -> Path:
    """Validate and package one diagnostic build; never flash or access USB."""
    diagnostic_manifest = diagnostic_manifest.expanduser().resolve(strict=True)
    nrftest_root = nrftest_root.expanduser().resolve(strict=True)
    manifest, hex_path, hex_sha, segments = _validate_manifest(
        diagnostic_manifest, nrftest_root
    )
    config = _load_package_config(nrftest_root)
    stock_manifest_path = (
        stock_manifest.expanduser().absolute()
        if stock_manifest is not None
        else nrftest_root / STOCK_BUILD_RELATIVE_PATH / STOCK_MANIFEST_NAME
    )
    stock, stock_package_path, stock_package_sha = _validate_stock(
        stock_manifest_path,
        _require_sha(expected_stock_manifest_sha256, "expected stock manifest SHA"),
        _require_sha(expected_stock_package_sha256, "expected stock package SHA"),
    )
    stock_hex_sha = _artifact_sha(stock, "zephyr/zephyr.hex")
    if hex_sha == stock_hex_sha:
        raise DiagnosticPackageError(
            "diagnostic HEX is identical to the immutable stock HEX"
        )
    stock_hex_path = _safe_relative_path(
        stock_manifest_path.parent.resolve(), "zephyr/zephyr.hex", "stock HEX"
    )
    if sha256_file(stock_hex_path) != stock_hex_sha:
        raise DiagnosticPackageError("stock HEX changed from its stock manifest")

    destination = output or diagnostic_manifest.parent / DIAGNOSTIC_PACKAGE_NAME
    destination = destination.expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    destination = destination.absolute()
    if destination.parent != diagnostic_manifest.parent:
        raise DiagnosticPackageError(
            "diagnostic package must be written beside its diagnostic manifest"
        )
    if (
        destination.name == stock_package_path.name
        or destination == stock_package_path.resolve()
    ):
        raise DiagnosticPackageError(
            "diagnostic package must have a distinct name from the stock package"
        )
    if destination.exists() or destination.is_symlink():
        raise DiagnosticPackageError(
            f"diagnostic package output already exists: {destination}"
        )
    normalized = destination.with_name(destination.name + ".normalized")
    if normalized.exists() or normalized.is_symlink():
        raise DiagnosticPackageError(
            f"diagnostic normalization output already exists: {normalized}"
        )

    tool = verify_tool(nrftest_root, nrfutil)
    selected_nrfutil = Path(cast(str, tool["executable"]))
    install_root = selected_nrfutil.parent.parent
    _invoke_tool(
        selected_nrfutil,
        cast(str, tool["command"]),
        [
            "pkg",
            "generate",
            "--hw-version",
            str(config.hardware_version),
            "--sd-req",
            config.softdevice_requirement,
            "--application",
            str(hex_path),
            "--application-version",
            str(config.application_version),
            str(destination),
        ],
        install_root,
        180,
    )
    if not destination.is_file() or destination.is_symlink():
        raise DiagnosticPackageError(
            f"nRF Util did not create the expected package: {destination}"
        )
    contents = normalize_package_zip(destination)
    _invoke_tool(
        selected_nrfutil,
        cast(str, tool["command"]),
        ["pkg", "display", str(destination)],
        install_root,
        60,
    )
    inspected_contents, embedded_manifest = _inspect_package(destination)
    if contents != inspected_contents:
        raise DiagnosticPackageError("normalized DFU ZIP changed during validation")

    package_sha = sha256_file(destination)
    manifest["dfu_package"] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "path": destination.relative_to(diagnostic_manifest.parent).as_posix(),
        "size": destination.stat().st_size,
        "sha256": package_sha,
        "format": config.format,
        "signed": config.signed,
        "container_normalized": True,
        "normalized_zip_timestamp": "1980-01-01T00:00:00",
        "parameters": {
            "hardware_version": config.hardware_version,
            "softdevice_requirement": config.softdevice_requirement,
            "application_version": config.application_version,
        },
        "contents": contents,
        "embedded_manifest": embedded_manifest,
        "tool": tool,
        "stock_reference": {
            "manifest": str(stock_manifest_path),
            "manifest_sha256": expected_stock_manifest_sha256,
            "package": str(stock_package_path),
            "package_sha256": stock_package_sha,
            "hex_sha256": stock_hex_sha,
        },
        "flash_segments": [
            {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
            for start, end in segments
        ],
    }
    try:
        diagnostic_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as error:
        raise DiagnosticPackageError(
            f"package validated but diagnostic manifest could not be updated: {error}"
        ) from error
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package-only",
        action="store_true",
        required=True,
        help="required explicit opt-in; this command never flashes hardware",
    )
    parser.add_argument(
        "--diagnostic-manifest", type=Path, default=DEFAULT_DIAGNOSTIC_MANIFEST
    )
    parser.add_argument("--nrftest-root", type=Path, required=True)
    parser.add_argument("--stock-manifest", type=Path)
    parser.add_argument("--nrfutil", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-stock-manifest-sha256")
    parser.add_argument("--expected-stock-package-sha256", default=STOCK_PACKAGE_SHA256)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = package(
            arguments.diagnostic_manifest,
            arguments.nrftest_root,
            stock_manifest=arguments.stock_manifest,
            nrfutil=arguments.nrfutil,
            output=arguments.output,
            expected_stock_manifest_sha256=(
                arguments.expected_stock_manifest_sha256 or STOCK_MANIFEST_SHA256
            ),
            expected_stock_package_sha256=arguments.expected_stock_package_sha256,
        )
    except (DiagnosticPackageError, OSError, UnicodeError) as error:
        print(f"diagnostic package error: {error}", file=sys.stderr)
        return 2
    print(f"Validated diagnostic package without flashing hardware: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
