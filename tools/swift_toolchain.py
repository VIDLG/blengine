from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import cast

from tools.local_config import ConfiguredPath, resolve_toolchain_root

VERSION = "6.3.3"
PACKAGE_ID = "Swift.Toolchain"
PACKAGE_SOURCE = "winget"
INSTALLER_URL = (
    "https://download.swift.org/swift-6.3.3-release/windows10/"
    "swift-6.3.3-RELEASE/swift-6.3.3-RELEASE-windows10.exe"
)
INSTALLER_SHA256 = "235626548f249cd516d3d4d90eee980dccad46f3822dac1f8e3119b0fede94b7"


def configured_install_root() -> ConfiguredPath:
    return resolve_toolchain_root("swift", "BLEHUB_SWIFT_ROOT")


def install_root() -> Path:
    return configured_install_root().path


def _require_windows() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("the managed Swift 6.3.3 toolchain package requires Windows")


def _winget() -> str:
    executable = shutil.which("winget.exe") or shutil.which("winget")
    if executable is None:
        raise RuntimeError("WinGet is required; install Microsoft App Installer first")
    return executable


def _vswhere() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Microsoft Visual Studio/Installer/vswhere.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Microsoft Visual Studio/Installer/vswhere.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Visual Studio Installer vswhere.exe was not found")


def _visual_studio_root() -> Path:
    result = subprocess.run(
        [
            str(_vswhere()),
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-latest",
            "-property",
            "installationPath",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Visual Studio x64 C++ build tools are not installed")
    return Path(output)


def _latest_file(root: Path, pattern: str, description: str) -> Path:
    matches = sorted(path for path in root.glob(pattern) if path.is_file())
    if not matches:
        raise RuntimeError(f"{description} was not found under {root}")
    return matches[-1]


def prerequisites() -> tuple[Path, Path]:
    _require_windows()
    visual_studio = _visual_studio_root()
    compiler = _latest_file(
        visual_studio,
        "VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe",
        "the x64 MSVC compiler",
    )
    windows_kits = (
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Windows Kits/10/bin"
    )
    resource_compiler = _latest_file(
        windows_kits,
        "*/x64/rc.exe",
        "the x64 Windows SDK resource compiler",
    )
    return compiler, resource_compiler


def _swiftc_candidates(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.rglob("swiftc.exe"))


def toolchain_environment(root: Path, tool_bin: Path) -> dict[str, str]:
    runtime_bin = root / "Runtimes" / VERSION / "usr" / "bin"
    if not runtime_bin.is_dir():
        raise RuntimeError(f"the Swift runtime directory is missing: {runtime_bin}")
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        [str(tool_bin), str(runtime_bin), environment.get("PATH", "")]
    )
    return environment


def sdk_root(root: Path | None = None) -> Path:
    selected_root = root or install_root()
    sdk = (
        selected_root
        / "Platforms"
        / VERSION
        / "Windows.platform"
        / "Developer"
        / "SDKs"
        / "Windows.sdk"
    )
    required = [
        sdk / "SDKSettings.json",
        sdk
        / "usr"
        / "lib"
        / "swift"
        / "windows"
        / "Swift.swiftmodule"
        / "x86_64-unknown-windows-msvc.swiftmodule",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"the Swift Windows SDK is incomplete:\n{joined}")
    return sdk


def _version_output(root: Path, executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        check=True,
        capture_output=True,
        text=True,
        env=toolchain_environment(root, executable.parent),
    )
    return result.stdout.strip() or result.stderr.strip()


def locate(root: Path | None = None) -> tuple[Path, Path, str]:
    selected_root = root or install_root()
    failures: list[str] = []
    for swiftc in _swiftc_candidates(selected_root):
        swift = swiftc.with_name("swift.exe")
        if not swift.is_file():
            failures.append(f"missing {swift}")
            continue
        try:
            version_output = _version_output(selected_root, swiftc)
        except (OSError, subprocess.CalledProcessError) as error:
            failures.append(f"{swiftc}: {error}")
            continue
        if f"Swift version {VERSION}" in version_output:
            return swiftc, swift, version_output
        failures.append(f"{swiftc}: {version_output}")

    detail = "\n".join(failures)
    suffix = f"\n{detail}" if detail else ""
    raise RuntimeError(f"Swift {VERSION} was not found under {selected_root}{suffix}")


def _verify_root(root: Path) -> tuple[Path, Path, str]:
    swiftc, swift, version_output = locate(root)
    _ = subprocess.run(
        [str(swiftc), "-sdk", str(sdk_root(root)), "-typecheck", "-"],
        check=True,
        input="let blehubSwiftToolchainProbe: UInt32 = 1\n",
        text=True,
        env=toolchain_environment(root, swiftc.parent),
    )
    return swiftc, swift, version_output


def verify() -> tuple[Path, Path, str]:
    _require_windows()
    _ = prerequisites()
    return _verify_root(install_root())


def install() -> Path:
    _require_windows()
    _ = prerequisites()
    root = install_root()

    try:
        _ = _verify_root(root)
        print(f"Swift {VERSION} is already installed; skipping WinGet: {root}")
        return root
    except RuntimeError:
        pass

    root.parent.mkdir(parents=True, exist_ok=True)
    custom = " ".join(
        [
            "InstallPerMachine=0",
            f'InstallRoot="{root}"',
            "OptionsInstallAndroidPlatform=0",
            "OptionsInstallWindowsSDKX86=0",
            "OptionsInstallWindowsSDKARM64=0",
        ]
    )
    _ = subprocess.run(
        [
            _winget(),
            "install",
            "--exact",
            "--id",
            PACKAGE_ID,
            "--version",
            VERSION,
            "--source",
            PACKAGE_SOURCE,
            "--scope",
            "user",
            "--architecture",
            "x64",
            "--silent",
            "--disable-interactivity",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--force",
            "--custom",
            custom,
        ],
        check=True,
    )
    _ = _verify_root(root)
    return root


def report() -> None:
    _require_windows()
    configured = configured_install_root()
    root = configured.path
    print(f"expected_version={VERSION}")
    print(f"root={root}")
    print(f"root_source={configured.source}")
    print(f"package_id={PACKAGE_ID}")
    print(f"package_source={PACKAGE_SOURCE}")
    print(f"installer_url={INSTALLER_URL}")
    print(f"installer_sha256={INSTALLER_SHA256}")

    try:
        compiler, resource_compiler = prerequisites()
        print("prerequisites=ready")
        print(f"msvc={compiler}")
        print(f"windows_sdk_rc={resource_compiler}")
    except RuntimeError as error:
        print("prerequisites=missing")
        print(f"prerequisite_error={error}")

    try:
        swiftc, swift, version_output = _verify_root(root)
        print("installed=true")
        print(f"swiftc={swiftc}")
        print(f"swift={swift}")
        print(f"sdk={sdk_root(root)}")
        print(f"detected_version={version_output}")
    except RuntimeError as error:
        print("installed=false")
        print(f"install_error={error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage BleHub's pinned shared Swift toolchain on Windows."
    )
    _ = parser.add_argument("command", choices=("install", "path", "verify", "report"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = cast(str, args.command)
    if command == "install":
        print(install())
    elif command == "path":
        swiftc, _, _ = verify()
        print(swiftc.parent)
    elif command == "verify":
        swiftc, _, version_output = verify()
        print(swiftc)
        print(version_output)
    else:
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
