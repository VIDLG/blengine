"""Validate a LOCAL DK build artifact; never assert what is installed on a DK.

This module uses only the standard library. ``validate_local_image(expected_sha)``
requires a caller-supplied SHA-256 and checks the pinned manifest, build inputs,
all artifacts, generated board policy and every Intel HEX data address. A pass
is NOT SWD readback or verification of installed firmware.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from tests.peripherals.nrf52840.fixture_attestation_wire import (
    DIGEST_SLOT_LENGTH,
    DIGEST_SLOT_MARKER,
)
from tests.peripherals.nrf52840.fixture_contract import (
    FixtureContractError,
    load_contract,
)

ROOT = Path(__file__).resolve().parents[4]
WORK = ROOT / ".work" / "nrf-att-hold-maintained-dk"
CONTRACT = (
    ROOT
    / "tests"
    / "conformance"
    / "scenarios"
    / "fixture-contracts"
    / "diagnostic-pending-read-v1.json"
)
PATCH = Path(__file__).parent / "firmware" / "0001-bttester-gatt-read-hold.patch"
PATCH_SHA256 = "d952912c21fe22a7474df7865b755b6c568fa7d0073a1360560c36a18d9bf40c"
BOARD_CONF_SHA256 = "358adfedbdf6cd02da94a7e1939f98424ee8fd5be27b01cb930a0906be0dba90"
BOARD_OVERLAY_SHA256 = (
    "5c2afdc65099afdadc833596c58034d7793e5e55e01bb02df6402c2da543acda"
)
HEAD = "b5f309bc2da57b49012648c9432d0d4cb5ee1cc8"
SDK_VERSION = "1.0.1"
BOARD = "nrf52840dk/nrf52840"
APP = Path("tests/bluetooth/tester")
EXTRA_CONFIG = (
    "CONFIG_BTTESTER_GATT_READ_HOLD=y\n"
    "CONFIG_BOOTLOADER_MCUBOOT=n\n"
    "CONFIG_USE_DT_CODE_PARTITION=n\n"
    "CONFIG_LOG_BACKEND_UART=n\n"
    "CONFIG_PRINTK=n\n"
    "CONFIG_BOOT_BANNER=n\n"
)
POLICY = {
    "CONFIG_BOARD": '"nrf52840dk"',
    "CONFIG_BOARD_TARGET": f'"{BOARD}"',
    "CONFIG_BOARD_NRF52840DK_NRF52840": "y",
    "CONFIG_SOC_NRF52840": "y",
    "CONFIG_BOARD_HAS_NRF5_BOOTLOADER": "n",
    "CONFIG_BOOTLOADER_MCUBOOT": "n",
    "CONFIG_USE_DT_CODE_PARTITION": "n",
    "CONFIG_FLASH_LOAD_OFFSET": "0",
    "CONFIG_ROM_START_OFFSET": "0",
    "CONFIG_FLASH_BASE_ADDRESS": "0x0",
    "CONFIG_FLASH_SIZE": "1024",
    "CONFIG_UART_PIPE": "y",
    "CONFIG_UART_CONSOLE": "n",
    "CONFIG_LOG_BACKEND_UART": "n",
    "CONFIG_SHELL_BACKEND_SERIAL": "n",
    "CONFIG_PRINTK": "n",
    "CONFIG_BOOT_BANNER": "n",
    "CONFIG_TEST": "n",
    "CONFIG_LOG": "y",
    "CONFIG_RTT_CONSOLE": "y",
    "CONFIG_LOG_BACKEND_RTT": "y",
    "CONFIG_LOG_BACKEND_RTT_MODE_DROP": "y",
    "CONFIG_BT": "y",
    "CONFIG_BT_PERIPHERAL": "y",
    "CONFIG_BT_GATT_DYNAMIC_DB": "y",
    "CONFIG_BT_HCI_HOST": "y",
    "CONFIG_BT_LL_SW_SPLIT": "y",
    "CONFIG_HAS_BT_CTLR": "y",
    "CONFIG_BT_CTLR_HCI": "y",
    "CONFIG_BT_SUBRATING": "n",
    "CONFIG_BTTESTER_GATT_READ_HOLD": "y",
    "CONFIG_BTTESTER_GATT_READ_HOLD_TIMEOUT_MS": "5000",
}
ARTIFACTS = (
    "zephyr/.config",
    "zephyr/zephyr.dts",
    "zephyr/zephyr.elf",
    "zephyr/zephyr.hex",
    "zephyr/zephyr.bin",
)
HEX_LIMIT = (
    0x100000  # DK: 1 MiB internal flash, offset zero; no UICR or external flash.
)


class ImageError(RuntimeError):
    """Local build artifact or provenance failed validation."""


@dataclass(frozen=True)
class LocalImage:
    """Validated local paths/hash only; not an installed-image attestation."""

    hex_path: Path
    manifest_path: Path
    sha256: str


def safe_path(path: Path) -> Path:
    """Reject linked paths, including any linked ancestor (even dangling links)."""
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ImageError(f"linked path refused: {component}")
    return path


def digest(path: Path) -> str:
    safe_path(path)
    if not path.is_file():
        raise ImageError(f"missing regular file: {path}")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def normalized_image_digest(path: Path) -> str:
    """Hash a binary image after zeroing its self-referential digest slot."""
    safe_path(path)
    if not path.is_file():
        raise ImageError(f"missing regular file: {path}")
    image = bytearray(path.read_bytes())
    offsets: list[int] = []
    offset = image.find(DIGEST_SLOT_MARKER)
    while offset >= 0:
        offsets.append(offset)
        offset = image.find(DIGEST_SLOT_MARKER, offset + 1)
    if len(offsets) != 1:
        raise ImageError(
            "firmware digest slot marker must occur exactly once; "
            + f"found {len(offsets)} in {path}"
        )
    slot_start = offsets[0] + len(DIGEST_SLOT_MARKER)
    slot_end = slot_start + DIGEST_SLOT_LENGTH
    slot = image[slot_start:slot_end]
    if len(slot) != DIGEST_SLOT_LENGTH or any(
        value not in b"0123456789abcdef" for value in slot
    ):
        raise ImageError("firmware digest slot is missing or not lowercase hexadecimal")
    image[slot_start:slot_end] = b"0" * DIGEST_SLOT_LENGTH
    return hashlib.sha256(image).hexdigest()


def tree_digest(directory: Path) -> str:
    safe_path(directory)
    if not directory.is_dir():
        raise ImageError(f"missing source tree: {directory}")
    h = hashlib.sha256()
    for entry in sorted(directory.rglob("*")):
        safe_path(entry)
        if entry.is_file():
            relative = entry.relative_to(directory).as_posix().encode("utf-8")
            h.update(len(relative).to_bytes(4, "big"))
            h.update(relative)
            h.update(bytes.fromhex(digest(entry)))
        elif not entry.is_dir():
            raise ImageError(f"unsafe source entry: {entry}")
    return h.hexdigest()


def config_values(path: Path) -> dict[str, str]:
    safe_path(path)
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", 1)
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            name, value = line[2:-11], "n"
        else:
            continue
        if name in values:
            raise ImageError(f"duplicate generated config: {name}")
        values[name] = value
    for name, expected in POLICY.items():
        if values.get(name, "n") != expected:
            raise ImageError(f"generated config mismatch: {name}")
    return {name: values.get(name, "n") for name in POLICY}


def hex_segments(path: Path) -> list[tuple[int, int]]:
    """Parse Intel HEX checksums, addressing and bytes without optional packages."""
    safe_path(path)
    ranges: list[tuple[int, int]] = []
    base = 0
    eof = False
    with path.open("r", encoding="ascii") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.rstrip("\r\n")
            if eof or not line.startswith(":") or len(line) < 11 or len(line) % 2 != 1:
                raise ImageError(f"invalid HEX record at line {number}")
            try:
                record = bytes.fromhex(line[1:])
            except ValueError as error:
                raise ImageError(f"invalid HEX bytes at line {number}") from error
            count, hi, lo, kind = record[:4]
            data = record[4:-1]
            if len(data) != count or sum(record) & 0xFF:
                raise ImageError(f"invalid HEX length/checksum at line {number}")
            offset = (hi << 8) | lo
            if kind == 0:
                start = base + offset
                end = start + count
                if count and (start < 0 or end > HEX_LIMIT):
                    raise ImageError(f"HEX outside DK flash: 0x{start:x}-0x{end:x}")
                if count:
                    ranges.append((start, end))
            elif kind == 1:
                if count or offset:
                    raise ImageError("invalid HEX EOF")
                eof = True
            elif kind in (2, 4):
                if count != 2 or offset:
                    raise ImageError("invalid HEX address extension")
                base = int.from_bytes(data, "big") << (4 if kind == 2 else 16)
            elif kind in (3, 5):
                if count != 4 or offset:
                    raise ImageError("invalid HEX start address")
                start_address = (
                    (int.from_bytes(data[:2], "big") << 4)
                    + int.from_bytes(data[2:], "big")
                    if kind == 3
                    else int.from_bytes(data, "big")
                )
                if start_address >= HEX_LIMIT:
                    raise ImageError("HEX entry point outside DK flash")
            else:
                raise ImageError(f"unsupported HEX record type: {kind}")
    if not eof or not ranges:
        raise ImageError("HEX missing EOF or application data")
    segments: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if segments and start < segments[-1][1]:
            raise ImageError("overlapping HEX data")
        if segments and start == segments[-1][1]:
            segments[-1] = (segments[-1][0], end)
        else:
            segments.append((start, end))
    return segments


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ImageError("expected lowercase SHA-256")
    return value


def validate_local_image(expected_sha: str, *, work: Path | None = None) -> LocalImage:
    """Return validated local HEX identity; NEVER claims a DK contains these bytes."""
    _sha(expected_sha)
    try:
        contract = load_contract(CONTRACT)
    except FixtureContractError as error:
        raise ImageError(f"diagnostic fixture contract is invalid: {error}") from error
    if work is None:
        work = WORK
    safe_path(work)
    build = work / "build"
    path = build / "diagnostic-dk-manifest.json"
    safe_path(path)
    if not path.is_file():
        raise ImageError(f"missing local DK manifest: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ImageError("DK manifest must be an object")
    if manifest.get("image_sha256") != expected_sha:
        raise ImageError("SHA-256 mismatch between caller and local manifest")
    expected_fields = {
        "schema_version": 1,
        "kind": "nrf-att-read-hold-dk-build",
        "image_id": contract.image_id,
        "image_version": contract.image_version,
        "image_sha256": expected_sha,
        "profile_id": contract.profile_id,
        "service_uuid": contract.service_uuid,
        "local_name": contract.local_name,
        "capabilities": sorted(contract.capabilities),
        "board": BOARD,
        "zephyr_head": HEAD,
        "application": APP.as_posix(),
        "source_repository": "https://github.com/VIDLG/zephyr.git",
        "board_config_sha256": BOARD_CONF_SHA256,
        "board_overlay_sha256": BOARD_OVERLAY_SHA256,
        "sdk_version": SDK_VERSION,
        "flash_performed": False,
    }
    for name, expected in expected_fields.items():
        if type(manifest.get(name)) is not type(expected) or manifest[name] != expected:
            raise ImageError(f"DK manifest mismatch: {name}")
    _sha(manifest.get("source_tree_sha256"))
    toolchain = manifest.get("build_toolchain")
    if not isinstance(toolchain, dict):
        raise ImageError("DK build toolchain identity is missing")
    if toolchain.get("source") == "nrftest-pixi":
        if set(toolchain) != {"source", "pixi_lock_sha256"}:
            raise ImageError("DK Pixi toolchain identity is malformed")
        _sha(toolchain.get("pixi_lock_sha256"))
    elif toolchain != {"source": "explicit-local"}:
        raise ImageError("DK build toolchain identity is unsupported")
    if digest(PATCH) != PATCH_SHA256:
        raise ImageError("owned patch hash changed")
    inputs = manifest.get("inputs")
    expected_inputs = {
        "patch": (PATCH, PATCH_SHA256),
        "staged_application": (work / APP, None),
        "board_config": (
            work / APP / "boards/nrf52840dk_nrf52840.conf",
            BOARD_CONF_SHA256,
        ),
        "board_overlay": (
            work / APP / "boards/nrf52840dk_nrf52840.overlay",
            BOARD_OVERLAY_SHA256,
        ),
        "extra_config": (work / "diagnostic-dk.conf", None),
    }
    if not isinstance(inputs, dict) or set(inputs) != set(expected_inputs):
        raise ImageError("DK manifest input set changed")
    for name, (input_path, pin) in expected_inputs.items():
        record = inputs[name]
        if not isinstance(record, dict) or record.get("path") != str(input_path):
            raise ImageError(f"DK input path mismatch: {name}")
        actual = (
            tree_digest(input_path)
            if name == "staged_application"
            else digest(input_path)
        )
        if _sha(record.get("sha256")) != actual or (pin is not None and actual != pin):
            raise ImageError(f"DK input hash mismatch: {name}")
    if (work / "diagnostic-dk.conf").read_text(encoding="utf-8") != EXTRA_CONFIG:
        raise ImageError("DK extra config changed")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(ARTIFACTS):
        raise ImageError("DK artifact set changed")
    for record, relative in zip(artifacts, ARTIFACTS, strict=True):
        artifact = build / relative
        if not isinstance(record, dict) or record.get("path") != relative:
            raise ImageError(f"DK artifact path mismatch: {relative}")
        actual = digest(artifact)
        size = artifact.stat().st_size
        if (
            _sha(record.get("sha256")) != actual
            or type(record.get("size")) is not int
            or record["size"] != size
            or size == 0
        ):
            raise ImageError(f"DK artifact changed: {relative}")
    if config_values(build / ARTIFACTS[0]) != manifest.get("validated_config"):
        raise ImageError("DK generated config differs from manifest")
    segments = hex_segments(build / "zephyr/zephyr.hex")
    if manifest.get("flash_segments") != [
        {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
        for start, end in segments
    ]:
        raise ImageError("DK HEX segments differ from manifest")
    actual_sha = normalized_image_digest(build / "zephyr/zephyr.bin")
    if actual_sha != expected_sha:
        raise ImageError(f"normalized image SHA-256 mismatch: {actual_sha}")
    return LocalImage(build / "zephyr/zephyr.hex", path, actual_sha)
