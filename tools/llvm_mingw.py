from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import uuid
import zipfile
from pathlib import Path

import pooch

VERSION = "20260826"
ARCHIVE_NAME = f"llvm-mingw-{VERSION}-ucrt-x86_64.zip"
ARCHIVE_URL = (
    f"https://github.com/mstorsjo/llvm-mingw/releases/download/{VERSION}/{ARCHIVE_NAME}"
)
ARCHIVE_SHA256 = "ae601f4e0f72bbdf441ad2df8bb16f037e2e9251559ea6b37b4057aef39c06c3"
CACHE_ROOT = pooch.os_cache("mobile-ffi-toolchain") / "llvm-mingw"
INSTALL_ROOT = CACHE_ROOT / f"llvm-mingw-{VERSION}-ucrt-x86_64"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compiler(root: Path) -> Path:
    return root / "bin" / "x86_64-w64-mingw32-clang.exe"


def verify(root: Path = INSTALL_ROOT) -> None:
    required = [
        _compiler(root),
        root / "bin" / "llvm-ar.exe",
        root / "bin" / "llvm-dlltool.exe",
        root / "include" / "windows.h",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"incomplete LLVM-MinGW installation:\n{joined}")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            output = (destination / member.filename).resolve()
            if os.path.commonpath((destination_resolved, output)) != str(
                destination_resolved
            ):
                raise RuntimeError(
                    f"unsafe path in LLVM-MinGW archive: {member.filename}"
                )
        bundle.extractall(destination)


def install() -> Path:
    if platform.system() != "Windows":
        raise RuntimeError("the pinned x86_64 LLVM-MinGW host package requires Windows")

    try:
        verify()
        return INSTALL_ROOT
    except RuntimeError:
        pass

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    archive = Path(
        pooch.retrieve(
            url=ARCHIVE_URL,
            known_hash=f"sha256:{ARCHIVE_SHA256}",
            fname=ARCHIVE_NAME,
            path=CACHE_ROOT / "downloads",
            progressbar=True,
        )
    )
    if _sha256(archive) != ARCHIVE_SHA256:
        raise RuntimeError(f"LLVM-MinGW archive hash mismatch: {archive}")

    staging = CACHE_ROOT / f".staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True)
        _safe_extract(archive, staging)
        candidates = [path for path in staging.iterdir() if path.is_dir()]
        if len(candidates) != 1:
            raise RuntimeError(f"unexpected LLVM-MinGW archive layout: {archive}")
        extracted = candidates[0]
        verify(extracted)

        if INSTALL_ROOT.exists():
            shutil.rmtree(INSTALL_ROOT)
        extracted.replace(INSTALL_ROOT)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    verify()
    return INSTALL_ROOT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the pinned LLVM-MinGW toolchain."
    )
    parser.add_argument("command", choices=("install", "path", "verify", "report"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "install" or args.command == "path":
        print(install())
    elif args.command == "verify":
        verify()
        print(INSTALL_ROOT)
    else:
        root = install()
        print(f"version={VERSION}")
        print(f"root={root}")
        print(f"compiler={_compiler(root)}")
        print(f"archive_sha256={ARCHIVE_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
