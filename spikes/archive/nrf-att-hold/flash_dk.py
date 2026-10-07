"""Explicit, fail-closed DK J-Link flashing for the isolated ATT read-hold image.

Run from nrftest's Pixi environment. This never installs tools, mass-erases,
uses the Dongle DFU path, or silently chooses a probe. Flashing replaces the
DK's current application, which this spike does not back up.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import build_dk as dk
from serial.tools import list_ports

MANIFEST = dk.WORK / "build" / "diagnostic-dk-manifest.json"
HEX = dk.WORK / "build" / "zephyr" / "zephyr.hex"
SEGGER_VID = 0x1366


class DKFlashError(RuntimeError):
    """A DK image, tool, probe, or explicit destructive consent is invalid."""


def _sha(value: object, description: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise DKFlashError(f"{description} must be lowercase SHA-256")
    return value


def _manifest() -> dict[str, object]:
    if MANIFEST.is_symlink() or not MANIFEST.is_file():
        raise DKFlashError(f"missing or linked DK build manifest: {MANIFEST}")
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise DKFlashError(f"invalid DK build manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise DKFlashError("DK build manifest must be an object")
    return cast(dict[str, object], manifest)


def validate_image(confirmed_sha256: str) -> str:
    """Check pinned DK provenance, all build inputs and artifacts, and HEX bounds."""
    if MANIFEST.parent != dk.WORK / "build" or dk.WORK.is_symlink():
        raise DKFlashError("DK build location is not the isolated work root")
    manifest = _manifest()
    for field, expected in (
        ("schema_version", 1),
        ("kind", "nrf-att-read-hold-dk-build"),
        ("board", dk.BOARD),
        ("zephyr_head", dk.PINNED_HEAD),
        ("flash_performed", False),
    ):
        if manifest.get(field) != expected or type(manifest.get(field)) is not type(
            expected
        ):
            raise DKFlashError(f"DK build manifest mismatch: {field}")
    if manifest.get("validated_config") != dk.REQUIRED_CONFIG:
        raise DKFlashError("DK build manifest config differs from board policy")
    root = manifest.get("zephyr_root")
    nrftest = manifest.get("nrftest_root")
    if not isinstance(root, str) or not isinstance(nrftest, str):
        raise DKFlashError("missing pinned Zephyr/nrftest roots")
    try:
        source = dk.shared.verify_source(Path(root), Path(nrftest))
    except (dk.DiagnosticBuildError, OSError) as error:
        raise DKFlashError(f"pinned DK source changed: {error}") from error
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise DKFlashError("missing DK build inputs")
    expected_inputs = {
        "application": source,
        "staged_application": dk.WORK / dk.APP,
        "board_config": source / "boards" / dk.BOARD_CONFIG,
        "board_overlay": source / "boards" / dk.BOARD_OVERLAY,
        "extra_config": dk.WORK / "diagnostic-dk.conf",
        "patch": dk.PATCH,
    }
    for name, path in expected_inputs.items():
        record = inputs.get(name)
        if not isinstance(record, dict) or record.get("path") != str(path):
            raise DKFlashError(f"DK build input path changed: {name}")
        if path.is_symlink() or not path.exists():
            raise DKFlashError(f"DK build input missing or linked: {name}")
        actual = dk.tree_sha256(path) if path.is_dir() else dk.shared.sha256_file(path)
        if actual != _sha(record.get("sha256"), name):
            raise DKFlashError(f"DK build input hash changed: {name}")
    artifacts = manifest.get("artifacts")
    expected_artifacts = (
        "zephyr/.config",
        "zephyr/zephyr.dts",
        "zephyr/zephyr.elf",
        "zephyr/zephyr.hex",
        "zephyr/zephyr.bin",
    )
    if not isinstance(artifacts, list) or [
        item.get("path") if isinstance(item, dict) else None for item in artifacts
    ] != list(expected_artifacts):
        raise DKFlashError("DK build artifact set changed")
    for record, relative in zip(artifacts, expected_artifacts, strict=True):
        path = MANIFEST.parent / relative
        if path.is_symlink() or not path.is_file():
            raise DKFlashError(f"DK build artifact missing or linked: {relative}")
        assert isinstance(record, dict)
        if dk.shared.sha256_file(path) != _sha(
            record.get("sha256"), relative
        ) or path.stat().st_size != record.get("size"):
            raise DKFlashError(f"DK build artifact changed: {relative}")
    config = dk.shared.parse_config(MANIFEST.parent / "zephyr" / ".config")
    try:
        dk.validate_config(config)
        segments = dk.validate_hex(HEX)
    except dk.DiagnosticBuildError as error:
        raise DKFlashError(str(error)) from error
    if manifest.get("flash_segments") != [
        {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
        for start, end in segments
    ]:
        raise DKFlashError("DK HEX segments differ from manifest")
    runners = MANIFEST.parent / "zephyr" / "runners.yaml"
    if runners.is_symlink() or not runners.is_file():
        raise DKFlashError("missing DK west runner configuration")
    runner_text = runners.read_text(encoding="utf-8")
    if (
        "- jlink\n" not in runner_text
        or "- --device=nRF52840_xxAA\n" not in runner_text
    ):
        raise DKFlashError("DK west runner does not identify nRF52840 J-Link")
    image_sha = dk.shared.sha256_file(HEX)
    if image_sha != _sha(confirmed_sha256, "--confirm-hex-sha256"):
        raise DKFlashError(f"DK HEX confirmation mismatch: {image_sha}")
    return image_sha


def verify_probe(serial: str) -> tuple[int, str, tuple[str, ...]]:
    """Require one SEGGER USB device; it may expose two distinct VCOM ports."""
    if not re.fullmatch(r"[0-9]+", serial):
        raise DKFlashError(
            "--probe-serial must be an explicit numeric J-Link USB serial"
        )
    ports = [
        port
        for port in list_ports.comports()
        if port.vid == SEGGER_VID
        and port.serial_number == serial
        and port.device.startswith("/dev/cu.usbmodem")
    ]
    if not 1 <= len(ports) <= 2:
        raise DKFlashError(
            f"expected one SEGGER probe with 1-2 VCOM ports for {serial}; "
            f"found {len(ports)} ports"
        )
    identities = {(port.pid, port.location) for port in ports}
    names = tuple(sorted(str(port.device) for port in ports))
    if len(identities) != 1 or len(set(names)) != len(names):
        raise DKFlashError("SEGGER VCOM ports do not identify one USB device")
    pid, location = identities.pop()
    if not pid or not location:
        raise DKFlashError("SEGGER VCOM has incomplete USB product/location identity")
    return pid, location, names


def flash(serial: str, confirmed_sha256: str, confirm_target: str) -> None:
    """Flash only the confirmed DK image via the selected J-Link, without mass erase."""
    if confirm_target != dk.BOARD:
        raise DKFlashError("--confirm-target must explicitly name nrf52840dk/nrf52840")
    image_sha = validate_image(confirmed_sha256)
    probe = verify_probe(serial)
    west = shutil.which("west")
    jlink = shutil.which("JLinkExe")
    if not west or not jlink:
        raise DKFlashError("nrftest Pixi west and official JLinkExe are required")
    if not Path(jlink).is_file():
        raise DKFlashError("JLinkExe executable missing")
    # Never infer the SWD target from a USB serial alone: external SWD headers
    # may be attached. The caller explicitly acknowledges the DK target.
    if verify_probe(serial) != probe or validate_image(confirmed_sha256) != image_sha:
        raise DKFlashError("DK probe or image changed before flashing")
    command = [
        west,
        "flash",
        "--build-dir",
        str(MANIFEST.parent),
        "--no-rebuild",
        "--runner",
        "jlink",
        "--dev-id",
        serial,
        "--dev-id-type",
        "serialno",
        "--no-erase",
        "--file",
        str(HEX),
        "--file-type",
        "hex",
        "--commander",
        jlink,
    ]
    print(f"DK J-Link {serial} VCOM {probe[2]}; HEX SHA-256 {image_sha}")
    print("Flashing replaces the DK application; no stock backup is available.")
    try:
        subprocess.run(
            command,
            cwd=Path(cast(str, _manifest()["zephyr_root"])).parent,
            check=True,
            timeout=180,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise DKFlashError(f"DK J-Link flash failed: {error}") from error
    if verify_probe(serial)[:2] != probe[:2]:
        raise DKFlashError(
            "DK USB identity changed after flash; BTP doctor still required"
        )
    print("J-Link command finished; firmware/BTP/RF behavior NOT yet validated.")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse explicit destructive target and image confirmations; fail closed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-serial", required=True)
    parser.add_argument("--confirm-hex-sha256", required=True)
    parser.add_argument("--confirm-target", required=True)
    args = parser.parse_args(argv)
    try:
        flash(args.probe_serial, args.confirm_hex_sha256, args.confirm_target)
    except (DKFlashError, OSError, UnicodeError, ValueError) as error:
        print(f"DK flash refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
