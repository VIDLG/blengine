"""Optional BleHub-owned DK ATT read-hold build. No install, flash or hardware.

The explicit build may read the already installed nrftest west workspace,
SDK and Pixi tools. It never imports nrftest Python, modifies those inputs,
installs tools, flashes hardware, or participates in a runtime gate. Failed
staging is retained for inspection; never automatically replaced.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import tomllib

try:
    from . import image
except ImportError:  # direct invocation from this directory
    import image  # type: ignore[no-redef]

BuildError = image.ImageError
ROOT = image.ROOT
WORK = image.WORK
APP = image.APP


@dataclass(frozen=True)
class ReferenceInputs:
    """Existing local build-time inputs; no nrftest Python API is imported."""

    zephyr: Path
    sdk: Path
    bin_dir: Path
    pixi_lock_sha256: str


def reference_inputs(reference: Path) -> ReferenceInputs:
    """Require the configured nrftest workspace and already installed Pixi tools."""
    image.safe_path(reference)
    if not reference.is_dir() or not (reference / "pixi.toml").is_file():
        raise BuildError("reference root is not an installed nrftest checkout")
    lock = reference / "pixi.lock"
    config = reference / "nrftest.local.toml"
    image.safe_path(config)
    if not config.is_file():
        raise BuildError("nrftest.local.toml must select the existing build inputs")
    document = tomllib.loads(config.read_text(encoding="utf-8"))
    paths = document.get("toolchains", {})
    if not isinstance(paths, dict):
        raise BuildError("nrftest toolchains configuration is malformed")
    values = []
    for name in ("zephyr_root", "zephyr_sdk_root"):
        value = paths.get(name)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise BuildError(f"nrftest.local.toml must contain absolute {name}")
        path = Path(value)
        image.safe_path(path)
        values.append(path)
    bin_dir = reference / ".pixi/envs/default/bin"
    image.safe_path(bin_dir)
    for name in ("west", "dtc", "cmake", "ninja", "gperf"):
        if not (bin_dir / name).is_file():
            raise BuildError(f"nrftest Pixi tool is not installed: {name}")
    return ReferenceInputs(*values, bin_dir, image.digest(lock))


def run(command: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=1800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = (
            error.stderr
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        raise BuildError(f"command failed: {command[:3]}: {detail}") from error
    if command[0].endswith("west") or "build" in command:
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            if "warning:" in line.lower():
                raise BuildError(f"build warning: {line}")
    return result.stdout.strip()


def verify_source(zephyr: Path) -> Path:
    image.safe_path(zephyr)

    if not zephyr.is_dir() or run(
        ["git", "rev-parse", "--show-toplevel"], cwd=zephyr
    ) != str(zephyr):
        raise BuildError("--zephyr-root must be the actual local Git root")
    if run(["git", "rev-parse", "HEAD"], cwd=zephyr) != image.HEAD:
        raise BuildError("Zephyr HEAD differs from pinned commit")
    if (
        run(["git", "remote", "get-url", "origin"], cwd=zephyr)
        != "https://github.com/VIDLG/zephyr.git"
    ):
        raise BuildError("Zephyr origin differs from pinned source")
    if run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            APP.as_posix(),
        ],
        cwd=zephyr,
    ):
        raise BuildError("Zephyr Tester source is not clean")
    source = zephyr / APP
    image.tree_digest(source)  # rejects all links inside the copied tree
    for name, pin in (
        ("nrf52840dk_nrf52840.conf", image.BOARD_CONF_SHA256),
        ("nrf52840dk_nrf52840.overlay", image.BOARD_OVERLAY_SHA256),
    ):
        if image.digest(source / "boards" / name) != pin:
            raise BuildError(f"pinned DK board input changed: {name}")
    return source


def verify_patch() -> None:
    if image.digest(image.PATCH) != image.PATCH_SHA256:
        raise BuildError("BleHub-owned diagnostic patch differs from pinned SHA-256")


def stage(source: Path, *, work: Path = WORK) -> None:
    """Copy only Tester and patch the copy; refuse every existing output/link."""
    if work != ROOT / ".work" / "nrf-att-hold-maintained-dk":
        raise BuildError("staging must be blehub/.work/nrf-att-hold-maintained-dk")
    image.safe_path(work)
    if work.exists() or work.is_symlink():
        raise BuildError(f"output already exists; refusing overwrite: {work}")
    verify_patch()
    work.mkdir(parents=True, exist_ok=False)
    destination = work / APP
    destination.parent.mkdir(parents=True)
    shutil.copytree(source, destination, symlinks=True)
    image.tree_digest(destination)
    config = destination / "prj.conf"
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    for setting in ("CONFIG_TEST=y", "CONFIG_BT_SUBRATING=y"):
        if sum(line.rstrip("\r\n") == setting for line in lines) != 1:
            raise BuildError(f"unexpected upstream Tester prj.conf: {setting}")
    config.write_text(
        "".join(
            line
            for line in lines
            if line.rstrip("\r\n") not in {"CONFIG_TEST=y", "CONFIG_BT_SUBRATING=y"}
        ),
        encoding="utf-8",
    )
    prefix = "--directory=.work/nrf-att-hold-maintained-dk"
    for args in (
        ("--check", "--whitespace=error-all"),
        ("--whitespace=error-all",),
        ("--reverse", "--check"),
    ):
        run(["git", "apply", *args, prefix, str(image.PATCH)], cwd=ROOT)
    if "config BTTESTER_GATT_READ_HOLD" not in (destination / "Kconfig").read_text(
        encoding="utf-8"
    ):
        raise BuildError("patched Kconfig lacks ATT hold option")
    (work / "diagnostic-dk.conf").write_text(image.EXTRA_CONFIG, encoding="utf-8")


def verify_sdk(sdk: Path) -> None:
    """Accept only an already installed, version-pinned ARM Zephyr SDK."""
    image.safe_path(sdk)

    if not sdk.is_dir():
        raise BuildError("--sdk-root must point to an installed Zephyr SDK")
    version_file = sdk / "sdk_version"
    image.safe_path(version_file)
    if (
        not version_file.is_file()
        or version_file.read_text(encoding="utf-8").strip() != image.SDK_VERSION
    ):
        raise BuildError(f"Zephyr SDK must be version {image.SDK_VERSION}")
    compiler = (
        sdk
        / "gnu/arm-zephyr-eabi/bin"
        / ("arm-zephyr-eabi-gcc.exe" if os.name == "nt" else "arm-zephyr-eabi-gcc")
    )
    if not image.safe_path(compiler).is_file():
        raise BuildError("Zephyr SDK ARM compiler is missing")


def verify_workspace(zephyr: Path, west: str) -> None:
    """Require every local west project to match its manifest revision before staging."""
    workspace = zephyr.parent
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(workspace / ".west/config", encoding="utf-8")
    except configparser.Error as error:
        raise BuildError("west workspace configuration is malformed") from error
    if (
        not config.has_section("manifest")
        or zephyr.name != "zephyr"
        or config.get("manifest", "path", fallback=None) != "zephyr"
        or config.get("manifest", "file", fallback="west.yml") != "west.yml"
    ):
        raise BuildError("west manifest must be the pinned Zephyr checkout")
    if run(["git", "status", "--porcelain=v1", "--", "west.yml"], cwd=zephyr):
        raise BuildError("Zephyr west manifest has local changes")
    listing = run([west, "list", "-f", "{name}|{abspath}|{revision}"], cwd=workspace)
    names: set[str] = set()
    paths: set[Path] = set()
    for line in listing.splitlines():
        parts = line.split("|", maxsplit=2)
        if len(parts) != 3 or not all(parts):
            raise BuildError("west project listing is malformed")
        name, location, revision = parts
        project = Path(location)
        image.safe_path(project)

        if (
            not project.is_absolute()
            or not project.is_dir()
            or not project.resolve().is_relative_to(workspace.resolve())
            or (name == "manifest" and project != zephyr)
            or name in names
            or project in paths
        ):
            raise BuildError(f"west project has unsafe or duplicate identity: {name}")
        if name != "manifest" and re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
            raise BuildError(f"west project has unpinned revision: {name}")
        names.add(name)
        paths.add(project)
        head = run(["git", "rev-parse", "HEAD"], cwd=project)
        expected = (
            image.HEAD
            if name == "manifest" and project == zephyr and revision == "HEAD"
            else run(
                ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=project
            )
        )
        if head != expected:
            raise BuildError(f"west project revision mismatch: {name}")
        if run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=project
        ):
            raise BuildError(f"west project has local changes: {name}")
    if not names or "manifest" not in names or zephyr not in paths:
        raise BuildError("west project listing lacks the pinned Zephyr manifest")


def build(
    zephyr: Path,
    sdk: Path,
    *,
    dtc: Path | None = None,
    reference: Path | None = None,
) -> Path:
    """Produce a manifest only after a complete pinned, local HEX validation."""
    source = verify_source(zephyr)
    verify_patch()
    verify_sdk(sdk)
    workspace_config = zephyr.parent / ".west" / "config"
    image.safe_path(workspace_config)

    if not workspace_config.is_file():
        raise BuildError("Zephyr must be in an already initialized west workspace")
    inputs = reference_inputs(reference) if reference is not None else None
    if inputs is not None and (zephyr != inputs.zephyr or sdk != inputs.sdk):
        raise BuildError("build paths differ from the configured nrftest reference")
    west = str(inputs.bin_dir / "west") if inputs else shutil.which("west")
    dtc_exe = (
        inputs.bin_dir / "dtc"
        if inputs is not None
        else dtc
        if dtc is not None
        else (Path(found) if (found := shutil.which("dtc")) else None)
    )
    if not west or dtc_exe is None or not image.safe_path(dtc_exe).is_file():
        raise BuildError(
            "preinstalled west and dtc are required; no tools are installed"
        )
    image.safe_path(Path(west))

    verify_workspace(zephyr, west)
    if WORK.exists() or WORK.is_symlink():
        raise BuildError(f"output already exists; refusing overwrite: {WORK}")
    original = image.tree_digest(source)
    stage(source)
    staged = image.tree_digest(WORK / APP)
    output = WORK / "build"
    output.mkdir(exist_ok=False)
    env = dict(os.environ)
    env.update(
        ZEPHYR_BASE=str(zephyr),
        ZEPHYR_TOOLCHAIN_VARIANT="zephyr",
        ZEPHYR_SDK_INSTALL_DIR=str(sdk),
        PATH=str(inputs.bin_dir if inputs is not None else dtc_exe.parent)
        + os.pathsep
        + env.get("PATH", ""),
    )
    run(
        [
            west,
            "build",
            "--board",
            image.BOARD,
            "--build-dir",
            str(output),
            str(WORK / APP),
            "--",
            f"-DEXTRA_CONF_FILE={WORK / 'diagnostic-dk.conf'}",
        ],
        cwd=zephyr.parent,
        env=env,
    )
    verify_workspace(zephyr, west)
    if verify_source(zephyr) != source or image.tree_digest(source) != original:
        raise BuildError("Zephyr source changed during build")
    verify_sdk(sdk)
    if (
        image.tree_digest(WORK / APP) != staged
        or image.digest(image.PATCH) != image.PATCH_SHA256
    ):
        raise BuildError("patched Tester or patch changed during build")
    if (WORK / "diagnostic-dk.conf").read_text(encoding="utf-8") != image.EXTRA_CONFIG:
        raise BuildError("extra configuration changed during build")
    config = image.config_values(output / "zephyr/.config")
    segments = image.hex_segments(output / "zephyr/zephyr.hex")
    manifest = {
        "schema_version": 1,
        "kind": "nrf-att-read-hold-dk-build",
        "board": image.BOARD,
        "zephyr_head": image.HEAD,
        "application": APP.as_posix(),
        "flash_performed": False,
        "source_repository": "https://github.com/VIDLG/zephyr.git",
        "source_tree_sha256": original,
        "board_config_sha256": image.BOARD_CONF_SHA256,
        "board_overlay_sha256": image.BOARD_OVERLAY_SHA256,
        "sdk_version": image.SDK_VERSION,
        "build_toolchain": (
            {"source": "nrftest-pixi", "pixi_lock_sha256": inputs.pixi_lock_sha256}
            if inputs is not None
            else {"source": "explicit-local"}
        ),
        "inputs": {
            name: {
                "path": str(path),
                "sha256": (
                    image.tree_digest(path)
                    if name == "staged_application"
                    else image.digest(path)
                ),
            }
            for name, path in (
                ("patch", image.PATCH),
                ("staged_application", WORK / APP),
                ("board_config", WORK / APP / "boards/nrf52840dk_nrf52840.conf"),
                ("board_overlay", WORK / APP / "boards/nrf52840dk_nrf52840.overlay"),
                ("extra_config", WORK / "diagnostic-dk.conf"),
            )
        },
        "validated_config": config,
        "flash_segments": [
            {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
            for start, end in segments
        ],
        "artifacts": [
            {
                "path": name,
                "size": (output / name).stat().st_size,
                "sha256": image.digest(output / name),
            }
            for name in image.ARTIFACTS
        ],
    }
    path = output / "diagnostic-dk-manifest.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    image.validate_local_image(image.digest(output / "zephyr/zephyr.hex"))
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference-root",
        type=Path,
        help="optional override for BleHub's local nrftest root",
    )
    args = parser.parse_args(argv)
    try:
        from tools.local_config import resolve_toolchain_root

        reference = (
            args.reference_root
            or resolve_toolchain_root("nrftest", "BLEHUB_NRFTEST_ROOT").path
        )
        inputs = reference_inputs(reference)
        manifest = build(inputs.zephyr, inputs.sdk, reference=reference)
    except (BuildError, OSError, UnicodeError, ValueError) as error:
        print(f"DK build refused: {error}", file=sys.stderr)
        return 2
    print(f"Validated LOCAL DK HEX: {manifest} (not installed-firmware verification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
