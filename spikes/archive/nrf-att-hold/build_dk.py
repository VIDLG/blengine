"""Build an isolated nRF52840 DK ATT read-hold Tester HEX; never flash or package.

Run with nrftest's Pixi Python/west/intelhex on PATH. J-Link VCOM carries
binary BTP on UART0 with hardware flow control; logs use J-Link RTT instead.
All output is confined to blehub/.work/nrf-att-hold-dk. A failed run is retained
and must be inspected/removed manually before trying again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import build_diagnostic as shared

PINNED_HEAD = shared.PINNED_HEAD
APP = shared.APP
PATCH = shared.PATCH
BLEHUB_ROOT = shared.BLEHUB_ROOT
WORK = BLEHUB_ROOT / ".work" / "nrf-att-hold-dk"
BOARD = "nrf52840dk/nrf52840"
BOARD_CONFIG = "nrf52840dk_nrf52840.conf"
BOARD_OVERLAY = "nrf52840dk_nrf52840.overlay"

# The copied app's boards/*.conf already enables RTT logging and disables TEST.
# Do not copy the Dongle's pca10059.conf: the DK has no assumed Nordic DFU/MBR.
EXTRA_CONFIG = (
    "CONFIG_BTTESTER_GATT_READ_HOLD=y\n"
    "CONFIG_BOOTLOADER_MCUBOOT=n\n"
    "CONFIG_USE_DT_CODE_PARTITION=n\n"
    "CONFIG_LOG_BACKEND_UART=n\n"
    "CONFIG_PRINTK=n\n"
    "CONFIG_BOOT_BANNER=n\n"
)

REQUIRED_CONFIG = {
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
}

DiagnosticBuildError = shared.DiagnosticBuildError


def tree_sha256(directory: Path) -> str:
    """Fingerprint paths and bytes of the entire Tester application tree."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DiagnosticBuildError(f"Tester tree contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(shared.sha256_file(path)))
    return digest.hexdigest()


def validate_board_inputs(config: Path, overlay: Path) -> None:
    """Reject a swapped board configuration or a non-UART0 binary transport."""
    board_conf = config.read_text(encoding="utf-8")
    board_dts = overlay.read_text(encoding="utf-8")
    for setting in ("CONFIG_TEST=n", "CONFIG_LOG=y", "CONFIG_LOG_BACKEND_RTT=y"):
        if setting not in board_conf.splitlines():
            raise DiagnosticBuildError(f"DK board config missing {setting}")
    if (
        "zephyr,uart-pipe = &uart0;" not in board_dts
        or "hw-flow-control;" not in board_dts
        or "&uart0 {" not in board_dts
    ):
        raise DiagnosticBuildError(
            "DK overlay must use UART0 with hardware flow control"
        )


def stage(source: Path, work: Path, patch: Path) -> Path:
    """Copy and patch only Tester in a new DK work root; never overwrite it."""
    if work != BLEHUB_ROOT / ".work" / "nrf-att-hold-dk":
        raise DiagnosticBuildError("staging must be blehub/.work/nrf-att-hold-dk")
    if work.parent.is_symlink() or work.exists() or work.is_symlink():
        raise DiagnosticBuildError(f"output already exists; refusing overwrite: {work}")
    work.mkdir(parents=True, exist_ok=False)
    destination = work / APP
    destination.parent.mkdir(parents=True)
    shutil.copytree(source, destination)
    prj_conf = destination / "prj.conf"
    project_config = prj_conf.read_text(encoding="utf-8")
    # DK board config supplies TEST=n (for full RTT logging); this chip has no
    # optional subrating. Remove conflicting prj.conf values only in the copy.
    for setting in ("CONFIG_TEST=y", "CONFIG_BT_SUBRATING=y"):
        if project_config.splitlines().count(setting) != 1:
            raise DiagnosticBuildError(f"unexpected Tester prj.conf: {setting}")
        project_config = project_config.replace(setting + "\n", "", 1)
    prj_conf.write_text(project_config, encoding="utf-8")
    targets = ("Kconfig", "src/btp_gatt.c")
    originals = {name: shared.sha256_file(source / name) for name in targets}
    prefix = "--directory=.work/nrf-att-hold-dk"
    shared.run(
        ["git", "apply", "--check", "--whitespace=error-all", prefix, str(patch)],
        cwd=BLEHUB_ROOT,
    )
    shared.run(
        ["git", "apply", "--whitespace=error-all", prefix, str(patch)],
        cwd=BLEHUB_ROOT,
    )
    shared.run(
        ["git", "apply", "--reverse", "--check", prefix, str(patch)], cwd=BLEHUB_ROOT
    )
    for name in targets:
        if shared.sha256_file(destination / name) == originals[name]:
            raise DiagnosticBuildError(
                f"patch did not modify copied Tester file: {name}"
            )
        if shared.sha256_file(source / name) != originals[name]:
            raise DiagnosticBuildError(
                f"original Tester source changed during patch: {name}"
            )
    if "config BTTESTER_GATT_READ_HOLD" not in (destination / "Kconfig").read_text(
        encoding="utf-8"
    ):
        raise DiagnosticBuildError(
            "diagnostic Kconfig option missing from patched copy"
        )
    config = work / "diagnostic-dk.conf"
    config.write_text(EXTRA_CONFIG, encoding="utf-8")
    return config


def run_west(command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    """Treat even nonfatal Kconfig warnings as an unsafe configuration."""
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            capture_output=True,
            timeout=1800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = (
            error.stderr
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        raise DiagnosticBuildError(f"DK west build failed: {detail}") from error
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if "warning:" in line.lower():
            raise DiagnosticBuildError(f"DK build warning (including Kconfig): {line}")


def validate_config(values: Mapping[str, str]) -> None:
    """Require DK identity, no bootloader offset, and RTT-only diagnostic output."""
    differences = [
        f"{name}: expected {expected}, got {values.get(name, 'n')}"
        for name, expected in REQUIRED_CONFIG.items()
        if values.get(name, "n") != expected
    ]
    if differences:
        raise DiagnosticBuildError(
            "generated .config violates DK policy: " + "; ".join(differences)
        )


def validate_segments(segments: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Accept only nRF52840 internal application flash [0, 0x100000)."""
    if not segments:
        raise DiagnosticBuildError("HEX contains no data")
    for start, end in segments:
        if start < 0 or start >= end or end > 0x100000:
            raise DiagnosticBuildError(
                f"HEX segment outside DK application flash: 0x{start:x}-0x{end:x}"
            )
    return list(segments)


def validate_hex(path: Path) -> list[tuple[int, int]]:
    """Use nrftest Pixi's intelhex parser, not a string scan of HEX records."""
    from intelhex import IntelHex  # pyright: ignore[reportMissingTypeStubs]

    try:
        return validate_segments(IntelHex(str(path)).segments())
    except (OSError, ValueError) as error:
        raise DiagnosticBuildError(f"invalid Intel HEX {path}: {error}") from error


def build(
    zephyr_root: Path, sdk_root: Path, nrftest_root: Path, dtc: Path | None = None
) -> Path:
    """Build only a patched DK image and publish provenance after validation."""
    source = shared.verify_source(zephyr_root, nrftest_root)
    zephyr_root = zephyr_root.resolve()
    nrftest_root = nrftest_root.resolve()
    sdk_root = sdk_root.resolve(strict=True)
    if not sdk_root.is_dir():
        raise DiagnosticBuildError(
            "--sdk-root must be an installed Zephyr SDK directory"
        )
    west = shutil.which("west")
    dtc_path = dtc.resolve(strict=True) if dtc is not None else shutil.which("dtc")
    if not west or not dtc_path or not Path(dtc_path).is_file():
        raise DiagnosticBuildError(
            "west and dtc must be available from nrftest Pixi (or supply --dtc)"
        )
    board_conf = source / "boards" / BOARD_CONFIG
    overlay = source / "boards" / BOARD_OVERLAY
    for path in (board_conf, overlay):
        if not path.is_file() or path.is_symlink():
            raise DiagnosticBuildError(f"missing DK Tester board input: {path}")
    validate_board_inputs(board_conf, overlay)
    shared.verify_patch(PATCH)
    if WORK.parent.is_symlink() or WORK.exists() or WORK.is_symlink():
        raise DiagnosticBuildError(f"output already exists; refusing overwrite: {WORK}")
    input_hashes = {
        "application": tree_sha256(source),
        "board_config": shared.sha256_file(board_conf),
        "board_overlay": shared.sha256_file(overlay),
        "patch": shared.sha256_file(PATCH),
    }
    config = stage(source, WORK, PATCH)
    staged_hash = tree_sha256(WORK / APP)
    output = WORK / "build"
    output.mkdir(exist_ok=False)
    environment = dict(os.environ)
    environment.update(
        ZEPHYR_BASE=str(zephyr_root),
        ZEPHYR_TOOLCHAIN_VARIANT="zephyr",
        ZEPHYR_SDK_INSTALL_DIR=str(sdk_root),
        PATH=str(Path(dtc_path).parent) + os.pathsep + environment.get("PATH", ""),
    )
    run_west(
        [
            west,
            "build",
            "--board",
            BOARD,
            "--build-dir",
            str(output),
            str(WORK / APP),
            "--",
            f"-DEXTRA_CONF_FILE={config}",
        ],
        cwd=zephyr_root.parent,
        env=environment,
    )
    zephyr = output / "zephyr"
    values = shared.parse_config(zephyr / ".config")
    validate_config(values)
    segments = validate_hex(zephyr / "zephyr.hex")
    if shared.verify_source(zephyr_root, nrftest_root) != source:
        raise DiagnosticBuildError("Tester source changed during build")
    if tree_sha256(source) != input_hashes["application"]:
        raise DiagnosticBuildError("Tester source changed during build")
    if tree_sha256(WORK / APP) != staged_hash:
        raise DiagnosticBuildError("staged Tester source changed during build")
    for key, path in (
        ("board_config", board_conf),
        ("board_overlay", overlay),
        ("patch", PATCH),
    ):
        if shared.sha256_file(path) != input_hashes[key]:
            raise DiagnosticBuildError(f"build input changed during build: {path}")
    manifest = {
        "schema_version": 1,
        "kind": "nrf-att-read-hold-dk-build",
        "board": BOARD,
        "zephyr_head": PINNED_HEAD,
        "zephyr_root": str(zephyr_root),
        "nrftest_root": str(nrftest_root),
        "application": APP.as_posix(),
        "inputs": {
            "application": {"path": str(source), "sha256": input_hashes["application"]},
            "staged_application": {"path": str(WORK / APP), "sha256": staged_hash},
            "board_config": {
                "path": str(board_conf),
                "sha256": input_hashes["board_config"],
            },
            "board_overlay": {
                "path": str(overlay),
                "sha256": input_hashes["board_overlay"],
            },
            "extra_config": {"path": str(config), "sha256": shared.sha256_file(config)},
            "patch": {"path": str(PATCH), "sha256": input_hashes["patch"]},
        },
        "validated_config": {key: values.get(key, "n") for key in REQUIRED_CONFIG},
        "flash_segments": [
            {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
            for start, end in segments
        ],
        "artifacts": [
            shared.artifact(zephyr / name, output)
            for name in (
                ".config",
                "zephyr.dts",
                "zephyr.elf",
                "zephyr.hex",
                "zephyr.bin",
            )
        ],
        "flash_performed": False,
    }
    manifest_path = output / "diagnostic-dk-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zephyr-root", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--nrftest-root", type=Path, required=True)
    parser.add_argument("--dtc", type=Path, help="optional managed dtc executable")
    args = parser.parse_args(argv)
    try:
        manifest = build(args.zephyr_root, args.sdk_root, args.nrftest_root, args.dtc)
    except (DiagnosticBuildError, OSError, UnicodeError) as error:
        print(f"DK build error: {error}", file=sys.stderr)
        return 2
    print(f"Validated DK HEX; manifest: {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
