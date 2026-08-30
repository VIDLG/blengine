from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

VERSION = "0.30.1"
CRATE = "boltffi_cli"
CRATES_IO_CHECKSUM = "feefcd412f1b0160931ccd26a5ec5a258cb699ee9660c032d7b8db3be5951aba"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN_ROOT = PROJECT_ROOT / ".work" / "toolchains" / "boltffi"
INSTALL_ROOT = TOOLCHAIN_ROOT / VERSION
CARGO_HOME = TOOLCHAIN_ROOT / "cargo-home"


def executable() -> Path:
    name = "boltffi.exe" if os.name == "nt" else "boltffi"
    return INSTALL_ROOT / "bin" / name


def _version_output(path: Path) -> str:
    result = subprocess.run(
        [str(path), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"BoltFFI CLI version check failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def verify() -> Path:
    path = executable()
    if not path.is_file():
        raise RuntimeError(
            f"official BoltFFI CLI {VERSION} is not installed at {path}; "
            "run `pixi run just boltffi-bootstrap`"
        )
    output = _version_output(path)
    if VERSION not in output:
        raise RuntimeError(
            f"unexpected BoltFFI CLI version at {path}: {output!r}; expected {VERSION}"
        )
    return path


def install() -> Path:
    path = executable()
    if path.is_file():
        return verify()

    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    CARGO_HOME.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["CARGO_HOME"] = str(CARGO_HOME)
    result = subprocess.run(
        [
            "cargo",
            "install",
            CRATE,
            "--version",
            f"={VERSION}",
            "--locked",
            "--root",
            str(INSTALL_ROOT),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to install official {CRATE} {VERSION}")
    return verify()


def report(*, require_installed: bool) -> None:
    path = executable()
    print(f"boltffi_crate={CRATE}")
    print(f"boltffi_version={VERSION}")
    print(f"boltffi_crates_io_checksum={CRATES_IO_CHECKSUM}")
    print(f"boltffi_install_root={INSTALL_ROOT}")
    print(f"boltffi_bootstrap_cargo_home={CARGO_HOME}")
    print(f"boltffi_executable={path}")
    print(f"boltffi_installed={str(path.is_file()).lower()}")
    if path.is_file() or require_installed:
        verified = verify()
        print(f"boltffi_version_output={_version_output(verified)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install and verify the pinned official BoltFFI CLI."
    )
    parser.add_argument("command", choices=("install", "verify", "report"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "install":
        path = install()
        print(f"boltffi_executable={path}")
    elif args.command == "verify":
        path = verify()
        print(f"boltffi_executable={path}")
    else:
        report(require_installed=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
