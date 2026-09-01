from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TypedDict, cast

import tomllib

ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = ROOT / ".work" / "cargo-target"
STAGE_DIR = ROOT / ".work" / "staged" / "windows"
VENDOR_DIR = STAGE_DIR / "vendor"
CONSUMER_DIR = ROOT / "tests" / "consumers" / "windows"
PACKAGE_MANIFESTS = (
    ("blehub", ROOT / "crates" / "blehub" / "Cargo.toml"),
    ("blehub-windows", ROOT / "crates" / "blehub-windows" / "Cargo.toml"),
)
VERSION = "0.0.0"


class CargoPackage(TypedDict):
    id: str
    name: str
    version: str
    source: str | None
    manifest_path: str


class CargoMetadata(TypedDict):
    workspace_root: str
    workspace_members: list[str]
    packages: list[CargoPackage]


def run(command: list[str], *, cwd: Path = ROOT) -> str:
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(TARGET_DIR)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="")
        raise SystemExit(completed.returncode)
    return completed.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack_package(archive: Path) -> Path:
    destination = VENDOR_DIR / f"{archive.stem}"
    with tempfile.TemporaryDirectory(dir=STAGE_DIR) as temporary:
        temporary_path = Path(temporary)
        with tarfile.open(archive, "r:gz") as package:
            root = temporary_path.resolve()
            for member in package.getmembers():
                target = (temporary_path / member.name).resolve()
                if root not in target.parents and target != root:
                    raise RuntimeError(f"unsafe package member: {member.name}")
            package.extractall(temporary_path, filter="data")
        extracted = temporary_path / archive.stem
        if not extracted.is_dir():
            raise RuntimeError(f"package root missing from {archive}")
        if destination.exists():
            shutil.rmtree(destination)
        _ = shutil.move(str(extracted), destination)

    files = {
        path.relative_to(destination).as_posix(): sha256(path)
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path.name != ".cargo-checksum.json"
    }
    checksum = {
        "files": files,
        "package": sha256(archive),
    }
    _ = (destination / ".cargo-checksum.json").write_text(
        json.dumps(checksum, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return destination


def require_table(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{context} is not a TOML table")
    return cast(dict[str, object], value)


def assert_packaged_dependency(package_dir: Path) -> None:
    manifest = cast(
        dict[str, object],
        tomllib.loads((package_dir / "Cargo.toml").read_text(encoding="utf-8")),
    )
    target = require_table(manifest.get("target"), "target")
    windows = require_table(target.get("cfg(windows)"), "target.cfg(windows)")
    dependencies = require_table(windows.get("dependencies"), "Windows dependencies")
    blehub = require_table(dependencies.get("blehub"), "Windows blehub dependency")
    if blehub.get("version") != "=0.0.0":
        raise RuntimeError("packaged blehub-windows does not pin blehub =0.0.0")
    if "path" in blehub:
        raise RuntimeError("packaged blehub-windows still contains a path dependency")


def refresh_consumer_lock() -> None:
    lock = CONSUMER_DIR / "Cargo.lock"
    lock.unlink(missing_ok=True)
    _ = run(["cargo", "generate-lockfile", "--offline"], cwd=CONSUMER_DIR)
    print("Windows staged consumer lock: REFRESHED")


def assert_consumer_source_isolation() -> None:
    metadata = cast(
        CargoMetadata,
        json.loads(
            run(
                [
                    "cargo",
                    "metadata",
                    "--format-version",
                    "1",
                    "--locked",
                    "--offline",
                ],
                cwd=CONSUMER_DIR,
            )
        ),
    )
    consumer_manifest = (CONSUMER_DIR / "Cargo.toml").resolve()
    vendor_root = VENDOR_DIR.resolve()
    source_roots = {
        (ROOT / "crates" / "blehub").resolve(),
        (ROOT / "crates" / "blehub-windows").resolve(),
    }

    if Path(metadata["workspace_root"]).resolve() != CONSUMER_DIR.resolve():
        raise RuntimeError(
            "Cargo metadata did not use the standalone Windows consumer workspace"
        )

    workspace_members = set(metadata["workspace_members"])
    if len(workspace_members) != 1:
        raise RuntimeError(
            "Windows consumer metadata contains unexpected workspace members"
        )

    expected_packages: dict[str, Path] = {
        name: (VENDOR_DIR / f"{name}-{VERSION}" / "Cargo.toml").resolve()
        for name, _ in PACKAGE_MANIFESTS
    }
    observed_packages: dict[str, Path] = {}
    for package in metadata["packages"]:
        manifest_path = Path(package["manifest_path"]).resolve()
        package_id = package["id"]
        if package_id in workspace_members:
            if manifest_path != consumer_manifest or package["source"] is not None:
                raise RuntimeError(
                    "the only path package must be the standalone consumer"
                )
            continue

        if package["source"] is None:
            raise RuntimeError(
                f"dependency {package['name']} unexpectedly resolved as a path package"
            )
        if not manifest_path.is_relative_to(vendor_root):
            raise RuntimeError(
                f"dependency {package['name']} resolved outside the staged directory source: {manifest_path}"
            )
        if any(manifest_path.is_relative_to(root) for root in source_roots):
            raise RuntimeError(
                f"dependency {package['name']} fell back to workspace source: {manifest_path}"
            )
        if package["version"] == VERSION and package["name"] in expected_packages:
            observed_packages[package["name"]] = manifest_path

    if observed_packages != expected_packages:
        raise RuntimeError(
            f"Windows consumer did not resolve the exact staged BleHub packages: {observed_packages}"
        )

    print("Windows staged source isolation: PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-consumer-lock",
        action="store_true",
        help="regenerate the tracked consumer lock after staging changed same-version crates",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_args()
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True)

    for name, manifest in PACKAGE_MANIFESTS:
        command = ["cargo"]
        if name == "blehub-windows":
            command.extend(
                [
                    "--config",
                    'patch.crates-io.blehub.path="crates/blehub"',
                ]
            )
        command.extend(
            [
                "package",
                "--quiet",
                "--locked",
                "--allow-dirty",
                "--no-verify",
                "--manifest-path",
                str(manifest),
            ]
        )
        _ = run(command)

    _ = run(
        [
            "cargo",
            "vendor",
            "--quiet",
            "--locked",
            "--versioned-dirs",
            str(VENDOR_DIR),
        ]
    )

    staged = {}
    for name, _ in PACKAGE_MANIFESTS:
        archive = TARGET_DIR / "package" / f"{name}-{VERSION}.crate"
        if not archive.is_file():
            raise RuntimeError(f"Cargo package archive was not created: {archive}")
        package_dir = unpack_package(archive)
        staged[name] = {
            "archive": archive.name,
            "sha256": sha256(archive),
        }
        if name == "blehub-windows":
            assert_packaged_dependency(package_dir)

    if arguments.refresh_consumer_lock:
        refresh_consumer_lock()
    assert_consumer_source_isolation()

    _ = (STAGE_DIR / "stage-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": VERSION,
                "packages": staged,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Windows staged crates: PASS")


if __name__ == "__main__":
    main()
