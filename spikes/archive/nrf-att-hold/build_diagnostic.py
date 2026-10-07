"""Build an isolated, opt-in ATT read-hold Tester image; never flash or package it.

Run with nrftest's Pixi Python/west/intelhex on PATH. All output is confined to
blehub/.work/nrf-att-hold; existing staging and build outputs are never removed.
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
from typing import cast

from intelhex import IntelHex  # pyright: ignore[reportMissingTypeStubs]

PINNED_HEAD = "b5f309bc2da57b49012648c9432d0d4cb5ee1cc8"
BOARD = "nrf52840dongle/nrf52840"
APP = Path("tests/bluetooth/tester")
BLEHUB_ROOT = Path(__file__).resolve().parents[2]
WORK = BLEHUB_ROOT / ".work" / "nrf-att-hold"
PATCH = (
    Path(__file__).resolve().parent / "firmware" / "0001-bttester-gatt-read-hold.patch"
)
REQUIRED_CONFIG = {
    "CONFIG_BOARD_HAS_NRF5_BOOTLOADER": "y",
    "CONFIG_BOOTLOADER_MCUBOOT": "n",
    "CONFIG_USE_DT_CODE_PARTITION": "n",
    "CONFIG_FLASH_LOAD_OFFSET": "0x1000",
    "CONFIG_UART_PIPE": "y",
    "CONFIG_UART_CONSOLE": "n",
    "CONFIG_CONSOLE": "n",
    "CONFIG_PRINTK": "n",
    "CONFIG_HWINFO": "y",
    "CONFIG_BOOT_BANNER": "n",
    "CONFIG_TEST_LOGGING_DEFAULTS": "n",
    "CONFIG_LOG": "n",
    "CONFIG_BT": "y",
    "CONFIG_BT_PERIPHERAL": "y",
    "CONFIG_BT_GATT_DYNAMIC_DB": "y",
    "CONFIG_BT_HCI": "y",
    "CONFIG_BT_HCI_HOST": "y",
    "CONFIG_BT_LL_SW_SPLIT": "y",
    "CONFIG_HAS_BT_CTLR": "y",
    "CONFIG_BT_CTLR_HCI": "y",
    "CONFIG_BTTESTER_GATT_READ_HOLD": "y",
}


class DiagnosticBuildError(RuntimeError):
    """A diagnostic build input, isolation boundary, or artifact is unsafe."""


def sha256_file(path: Path) -> str:
    """Hash a build input or artifact, rejecting unreadable files."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise DiagnosticBuildError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def run(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> str:
    """Run a bounded, noninteractive subprocess and preserve useful failure text."""
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
        raise DiagnosticBuildError(
            f"{' '.join(command[:3])} failed: {detail}"
        ) from error
    return result.stdout.strip()


def verify_source(zephyr_root: Path, nrftest_root: Path) -> Path:
    """Require the exact Git checkout and an untouched Tester subtree in west."""
    zephyr_root = zephyr_root.resolve(strict=True)
    nrftest_root = nrftest_root.resolve(strict=True)
    if not (nrftest_root / "pixi.toml").is_file():
        raise DiagnosticBuildError("--nrftest-root is not the nrftest project")
    if not (zephyr_root.parent / ".west" / "config").is_file():
        raise DiagnosticBuildError(
            "Zephyr is not inside its initialized west workspace"
        )
    if run(["git", "rev-parse", "--show-toplevel"], cwd=zephyr_root) != str(
        zephyr_root
    ):
        raise DiagnosticBuildError("--zephyr-root must be the actual Zephyr Git root")
    head = run(["git", "rev-parse", "HEAD"], cwd=zephyr_root)
    if head != PINNED_HEAD:
        raise DiagnosticBuildError(f"Zephyr HEAD {head} is not pinned {PINNED_HEAD}")
    status = run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            APP.as_posix(),
        ],
        cwd=zephyr_root,
    )
    if status:
        raise DiagnosticBuildError("original Zephyr Tester subtree is not clean")
    source = zephyr_root / APP
    if not source.is_dir() or source.is_symlink():
        raise DiagnosticBuildError(f"Tester source is missing or linked: {source}")
    # No copied source entry may escape the pinned tree through a symlink.
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise DiagnosticBuildError(f"Tester subtree contains a symlink: {entry}")
    return source


def verify_patch(path: Path) -> None:
    """Reject patch additions outside the two expected Tester source files."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DiagnosticBuildError(f"cannot read patch {path}: {error}") from error
    expected = {
        "tests/bluetooth/tester/Kconfig",
        "tests/bluetooth/tester/src/btp_gatt.c",
    }
    headers = [line for line in text.splitlines() if line.startswith("diff --git ")]
    if not headers or set(headers) != {
        f"diff --git a/{name} b/{name}" for name in expected
    }:
        raise DiagnosticBuildError(
            "patch does not target exactly the expected Tester files"
        )
    if "GIT binary patch" in text or "\nrename " in text or "\ncopy " in text:
        raise DiagnosticBuildError("binary, rename, or copy patches are not supported")


def check_empty_outputs(work: Path) -> None:
    """Fail closed rather than replacing a prior run or following an output link."""
    for path in (work.parent, work, work / "tests", work / "tests" / "bluetooth"):
        if path.is_symlink():
            raise DiagnosticBuildError(f"output directory is a symlink: {path}")
    for path in (work / APP, work / "build", work / "diagnostic.conf"):
        if path.exists() or path.is_symlink():
            raise DiagnosticBuildError(
                f"output already exists; refusing overwrite: {path}"
            )


def stage(source: Path, work: Path, patch: Path, canonical_config: Path) -> Path:
    """Patch only copied Tester; retain failed staging, never auto-delete it."""
    try:
        relative_work = work.relative_to(BLEHUB_ROOT)
    except ValueError as error:
        raise DiagnosticBuildError(
            "staging must be inside the BleHub work directory"
        ) from error
    if relative_work.parts != (".work", "nrf-att-hold"):
        raise DiagnosticBuildError("staging must be blehub/.work/nrf-att-hold")
    check_empty_outputs(work)
    work.mkdir(parents=True, exist_ok=True)
    destination = work / APP
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False)
    targets = ("Kconfig", "src/btp_gatt.c")
    originals = {name: sha256_file(source / name) for name in targets}
    prefix = f"--directory={relative_work.as_posix()}"
    # Run from the repo root: git apply interprets paths relative to that root,
    # even when cwd is a nested ignored .work directory.
    run(
        ["git", "apply", "--check", "--whitespace=error-all", prefix, str(patch)],
        cwd=BLEHUB_ROOT,
    )
    run(["git", "apply", "--whitespace=error-all", prefix, str(patch)], cwd=BLEHUB_ROOT)
    run(["git", "apply", "--reverse", "--check", prefix, str(patch)], cwd=BLEHUB_ROOT)
    for name in targets:
        if sha256_file(destination / name) == originals[name]:
            raise DiagnosticBuildError(
                f"patch did not modify copied Tester file: {name}"
            )
        if sha256_file(source / name) != originals[name]:
            raise DiagnosticBuildError(
                f"original Tester source changed during patch: {name}"
            )
    if "config BTTESTER_GATT_READ_HOLD" not in (destination / "Kconfig").read_text(
        encoding="utf-8"
    ):
        raise DiagnosticBuildError(
            "diagnostic Kconfig option missing from patched copy"
        )
    config = work / "diagnostic.conf"
    config.write_bytes(
        canonical_config.read_bytes().rstrip(b"\n")
        + b"\nCONFIG_BTTESTER_GATT_READ_HOLD=y\n"
    )
    return config


def parse_config(path: Path) -> dict[str, str]:
    """Parse enabled and explicitly disabled generated Kconfig assignments."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line[2:-11]] = "n"
    return values


def validate_config(values: Mapping[str, str]) -> None:
    """Validate bootloader/transport policy AND the opt-in diagnostic switch."""
    differences = [
        f"{name}: expected {expected}, got {values.get(name, 'n')}"
        for name, expected in REQUIRED_CONFIG.items()
        if values.get(name, "n") != expected
    ]
    if differences:
        raise DiagnosticBuildError(
            "generated .config violates safety policy: " + "; ".join(differences)
        )


def validate_segments(segments: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Accept only application flash [0x1000, 0xe0000), never UICR/MBR/bootloader."""
    if not segments:
        raise DiagnosticBuildError("HEX contains no data")
    for start, end in segments:
        if start < 0x1000 or end > 0xE0000 or start >= end:
            raise DiagnosticBuildError(
                f"HEX segment outside application flash: 0x{start:x}-0x{end:x}"
            )
    return list(segments)


def validate_hex(path: Path) -> list[tuple[int, int]]:
    """Use nrftest's Pixi-provided intelhex parser to validate all address ranges."""
    try:
        image = IntelHex(str(path))
        segments = cast(list[tuple[int, int]], image.segments())
    except (OSError, ValueError) as error:
        raise DiagnosticBuildError(f"invalid Intel HEX {path}: {error}") from error
    return validate_segments(segments)


def artifact(path: Path, build: Path) -> dict[str, object]:
    """Identify a generated file by relative path, byte length, and SHA-256."""
    if not path.is_file():
        raise DiagnosticBuildError(f"missing build artifact: {path}")
    return {
        "path": path.relative_to(build).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build(
    zephyr_root: Path, sdk_root: Path, nrftest_root: Path, dtc: Path | None = None
) -> Path:
    """Build only the patched copy; verify artifacts before publishing a manifest."""
    source = verify_source(zephyr_root, nrftest_root)
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
    config_source = nrftest_root / "firmware" / "app" / "pca10059.conf"
    overlay = nrftest_root / "firmware" / "app" / "pca10059.overlay"
    for path in (config_source, overlay):
        if not path.is_file():
            raise DiagnosticBuildError(f"missing canonical input: {path}")
    verify_patch(PATCH)
    check_empty_outputs(WORK)
    hashes = {
        name: sha256_file(path)
        for name, path in (
            ("canonical_config", config_source),
            ("overlay", overlay),
            ("patch", PATCH),
        )
    }
    config = stage(source, WORK, PATCH, config_source)
    output = WORK / "build"
    output.mkdir(exist_ok=False)
    environment = dict(os.environ)
    environment.update(
        ZEPHYR_BASE=str(zephyr_root.resolve()),
        ZEPHYR_TOOLCHAIN_VARIANT="zephyr",
        ZEPHYR_SDK_INSTALL_DIR=str(sdk_root),
        PATH=str(Path(dtc_path).parent) + os.pathsep + environment.get("PATH", ""),
    )
    run(
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
            f"-DDTC_OVERLAY_FILE={overlay}",
        ],
        cwd=zephyr_root.resolve().parent,
        env=environment,
    )
    values = parse_config(output / "zephyr" / ".config")
    validate_config(values)
    hex_path = output / "zephyr" / "zephyr.hex"
    segments = validate_hex(hex_path)
    verify_source(zephyr_root, nrftest_root)
    for name, path in (
        ("canonical_config", config_source),
        ("overlay", overlay),
        ("patch", PATCH),
    ):
        if sha256_file(path) != hashes[name]:
            raise DiagnosticBuildError(f"build input changed during build: {path}")
    manifest = {
        "schema_version": 1,
        "kind": "nrf-att-read-hold-diagnostic",
        "board": BOARD,
        "zephyr_head": PINNED_HEAD,
        "zephyr_root": str(zephyr_root.resolve()),
        "nrftest_root": str(nrftest_root.resolve()),
        "application": APP.as_posix(),
        "inputs": {
            "canonical_config": {
                "path": str(config_source.resolve()),
                "sha256": hashes["canonical_config"],
            },
            "diagnostic_config": {"path": str(config), "sha256": sha256_file(config)},
            "overlay": {"path": str(overlay.resolve()), "sha256": hashes["overlay"]},
            "patch": {"path": str(PATCH.resolve()), "sha256": hashes["patch"]},
        },
        "validated_config": {key: values.get(key, "n") for key in REQUIRED_CONFIG},
        "flash_segments": [
            {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
            for start, end in segments
        ],
        "artifacts": [
            artifact(output / "zephyr" / name, output)
            for name in ("zephyr.elf", "zephyr.hex", "zephyr.bin")
        ],
        "dfu_package": None,  # Build-only diagnostic: no ZIP, signing, or flash claim.
    }
    manifest_path = output / "diagnostic-manifest.json"
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
        print(f"diagnostic build error: {error}", file=sys.stderr)
        return 2
    print(f"Validated diagnostic image; manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
