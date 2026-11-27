from __future__ import annotations

import argparse
import platform
import re
import shutil
import subprocess
from pathlib import Path

VERSION = "2.46.0"


def _required_executable(name: str) -> Path:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"required executable was not found on PATH: {name}")
    return Path(executable).resolve()


def verify() -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError(
            "XcodeGen is required only for Apple device development on macOS"
        )
    executable = _required_executable("xcodegen")
    result = subprocess.run(
        [str(executable), "--version"],
        stdin=subprocess.DEVNULL,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = (result.stdout or result.stderr).strip()
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    if match is None:
        raise RuntimeError(f"unexpected XcodeGen version output: {output!r}")
    actual = match.group(1)
    if actual != VERSION:
        raise RuntimeError(
            f"XcodeGen {VERSION} is required, but {actual} is installed at {executable}"
        )
    print(f"xcodegen={executable}")
    print(f"xcodegen_version={actual}")
    return executable


def install() -> Path:
    if platform.system() != "Darwin":
        raise RuntimeError("XcodeGen installation is supported only on macOS")
    try:
        return verify()
    except RuntimeError as error:
        if "was not found on PATH" not in str(error):
            raise

    brew = _required_executable("brew")
    subprocess.run([str(brew), "install", "xcodegen"], check=True)
    return verify()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the shared XcodeGen tool")
    _ = parser.add_argument("command", choices=("install", "verify"))
    args = parser.parse_args()
    command = getattr(args, "command", None)
    if command == "install":
        _ = install()
    else:
        _ = verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
