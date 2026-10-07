from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.local_config import ConfiguredPath, resolve_android_sdk_root

ANDROID_CLI_VERSION_POLICY = "official-current"
AVD_NAME = "medium_phone"
AVD_PROFILE = "medium_phone"


@dataclass(frozen=True)
class AndroidPackage:
    package: str
    version: str
    directory: str
    required_files: tuple[str, ...]

    @property
    def specification(self) -> str:
        return f"{self.package}@{self.version}"


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    state: str
    details: str

    @property
    def is_emulator(self) -> bool:
        return self.serial.startswith("emulator-")

    @property
    def kind(self) -> str:
        return "emulator" if self.is_emulator else "physical"


def _host_executable(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _ndk_host_tag() -> str:
    if os.name == "nt":
        return "windows-x86_64"
    if sys.platform == "darwin":
        return "darwin-x86_64"
    return "linux-x86_64"


def _emulator_abi() -> str:
    if sys.platform == "darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }:
        return "arm64-v8a"
    return "x86_64"


def _android_packages() -> tuple[AndroidPackage, ...]:
    emulator_abi = _emulator_abi()
    system_image = f"system-images/android-36/google_apis_playstore/{emulator_abi}"
    ndk_clang = (
        f"toolchains/llvm/prebuilt/{_ndk_host_tag()}/bin/{_host_executable('clang')}"
    )
    return (
        AndroidPackage(
            "cmdline-tools/latest",
            "23.0.0",
            "cmdline-tools/latest",
            (f"bin/{_host_executable('sdkmanager')}",),
        ),
        AndroidPackage(
            "platform-tools",
            "37.0.1",
            "platform-tools",
            (_host_executable("adb"),),
        ),
        AndroidPackage(
            "build-tools/36.0.0",
            "36.0.0",
            "build-tools/36.0.0",
            (_host_executable("aapt2"),),
        ),
        AndroidPackage(
            "platforms/android-36",
            "2.0.0",
            "platforms/android-36",
            ("android.jar",),
        ),
        AndroidPackage(
            "ndk/28.2.13676358",
            "28.2.13676358",
            "ndk/28.2.13676358",
            (ndk_clang,),
        ),
        AndroidPackage(
            "emulator",
            "37.1.11",
            "emulator",
            (_host_executable("emulator"),),
        ),
        AndroidPackage(
            system_image,
            "7.0.0",
            system_image,
            ("system.img",),
        ),
    )


PACKAGES = _android_packages()


def configured_sdk_root() -> ConfiguredPath:
    return resolve_android_sdk_root()


def sdk_root() -> Path:
    return configured_sdk_root().path


def android_executable() -> Path:
    local_executable = Path.home() / ".local" / "bin" / _host_executable("android")
    executable = (
        str(local_executable)
        if local_executable.is_file()
        else shutil.which("android.exe") or shutil.which("android")
    )
    if executable is None:
        if sys.platform == "darwin":
            instruction = (
                "install the official macOS CLI from "
                "https://dl.google.com/android/cli/latest/darwin_arm64/install.sh"
            )
        elif os.name == "nt":
            instruction = "install it with `winget install -e --id Google.AndroidCLI`"
        else:
            instruction = (
                "install the official Linux CLI from "
                "https://dl.google.com/android/cli/latest/linux_x86_64/install.sh"
            )
        raise RuntimeError(f"Google Android CLI is required; {instruction}")
    return Path(executable).resolve()


def _run_output(command: list[str]) -> tuple[int, str]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    return result.returncode, output


def verify_android_cli() -> Path:
    executable = android_executable()
    returncode, output = _run_output([str(executable), "--version"])
    if returncode != 0:
        raise RuntimeError(
            f"Android CLI version check failed with exit code {returncode}: {output}"
        )
    # The official CLI is intentionally rolling; validate that it runs rather
    # than pinning a build number that would block routine SDK maintenance.
    if not any(line[:1].isdigit() for line in output.splitlines()):
        raise RuntimeError(
            f"unexpected Android CLI version output {output.strip()!r}; "
            "expected a numeric version line"
        )
    return executable


def _properties(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _normalized_revision(value: str) -> tuple[int, ...] | None:
    try:
        parts = [int(part) for part in value.split(".")]
    except ValueError:
        return None
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def package_problems(package: AndroidPackage, root: Path) -> list[str]:
    directory = root / Path(package.directory)
    problems: list[str] = []
    if not directory.is_dir():
        return [f"missing directory {directory}"]

    properties_path = directory / "source.properties"
    properties = _properties(properties_path)
    revision = properties.get("Pkg.Revision")
    if revision is None:
        problems.append(f"missing Pkg.Revision in {properties_path}")
    elif _normalized_revision(revision) != _normalized_revision(package.version):
        problems.append(
            f"{package.package} revision is {revision}, expected {package.version}"
        )

    for relative in package.required_files:
        required = directory / Path(relative)
        if not required.is_file():
            problems.append(f"missing required file {required}")
    return problems


def verify_packages() -> None:
    root = sdk_root()
    failures: list[str] = []
    for package in PACKAGES:
        failures.extend(package_problems(package, root))
    if failures:
        joined = "\n  - ".join(failures)
        raise RuntimeError(
            f"Android SDK at {root} is incomplete:\n  - {joined}\n"
            "run `pixi run just android-bootstrap`"
        )


def toolchain_environment() -> dict[str, str]:
    verify_android_cli()
    verify_packages()
    root = sdk_root()
    environment = os.environ.copy()
    environment["ANDROID_HOME"] = str(root)
    environment["ANDROID_SDK_ROOT"] = str(root)
    environment["ANDROID_NDK_HOME"] = str(root / "ndk" / "28.2.13676358")
    environment["ANDROID_AVD_HOME"] = str(root / "avd")
    path_entries = (
        root / "platform-tools",
        root / "emulator",
        root / "cmdline-tools" / "latest" / "bin",
    )
    environment["PATH"] = os.pathsep.join(
        [*(str(path) for path in path_entries), environment.get("PATH", "")]
    )
    return environment


def _avd_names(environment: dict[str, str]) -> set[str]:
    executable = android_executable()
    returncode, output = _run_output_with_environment(
        [str(executable), f"--sdk={sdk_root()}", "emulator", "list"],
        environment,
    )
    if returncode != 0:
        raise RuntimeError(
            f"Android CLI could not list AVDs (exit {returncode}): {output}"
        )
    return {line.strip() for line in output.splitlines() if line.strip()}


def _run_output_with_environment(
    command: list[str], environment: dict[str, str]
) -> tuple[int, str]:
    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    return result.returncode, output


def create_avd() -> None:
    environment = toolchain_environment()
    if AVD_NAME in _avd_names(environment):
        print(f"android_avd={AVD_NAME}")
        print("android_avd_created=false")
        return

    executable = android_executable()
    result = subprocess.run(
        [
            str(executable),
            f"--sdk={sdk_root()}",
            "emulator",
            "create",
            AVD_PROFILE,
        ],
        env=environment,
        check=False,
    )
    if AVD_NAME not in _avd_names(environment):
        raise RuntimeError(
            f"Android CLI exited with {result.returncode} without creating AVD {AVD_NAME}"
        )
    print(f"android_avd={AVD_NAME}")
    print("android_avd_created=true")


def adb_executable() -> Path:
    name = _host_executable("adb")
    managed = sdk_root() / "platform-tools" / name
    if managed.is_file():
        return managed
    resolved = shutil.which(name)
    return Path(resolved).resolve() if resolved is not None else managed


def adb_environment() -> dict[str, str]:
    executable = adb_executable()
    if not executable.is_file():
        raise RuntimeError(
            "Android SDK Platform-Tools are unavailable; install the project SDK or "
            "put the official `adb` executable on PATH"
        )
    returncode, output = _run_output([str(executable), "version"])
    expected = next(
        package.version for package in PACKAGES if package.package == "platform-tools"
    )
    if returncode != 0 or not any(
        line.startswith(f"Version {expected}") for line in output.splitlines()
    ):
        raise RuntimeError(
            f"unexpected adb version output {output.strip()!r}; expected {expected}"
        )
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        (str(executable.parent), environment.get("PATH", ""))
    )
    return environment


def _connected_devices(environment: dict[str, str]) -> list[AndroidDevice]:
    returncode, output = _run_output_with_environment(
        [str(adb_executable()), "devices", "-l"], environment
    )
    if returncode != 0:
        raise RuntimeError(f"adb devices failed with exit code {returncode}: {output}")

    devices: list[AndroidDevice] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "List" or parts[0].startswith("*"):
            continue
        devices.append(
            AndroidDevice(
                serial=parts[0],
                state=parts[1],
                details=" ".join(parts[2:]),
            )
        )
    return devices


def _running_emulators(environment: dict[str, str]) -> list[str]:
    return [
        device.serial
        for device in _connected_devices(environment)
        if device.is_emulator and device.state == "device"
    ]


def _device_summary(devices: list[AndroidDevice]) -> str:
    if not devices:
        return "none"
    return ", ".join(
        f"{device.serial} ({device.kind}, {device.state})" for device in devices
    )


def select_test_device(
    mode: str = "auto", *, environment: dict[str, str] | None = None
) -> AndroidDevice:
    if mode not in {"auto", "physical", "emulator"}:
        raise RuntimeError(f"unsupported Android device mode {mode!r}")

    environment = environment or toolchain_environment()
    devices = _connected_devices(environment)
    explicit_serial = environment.get("BLEHUB_ANDROID_SERIAL") or environment.get(
        "ANDROID_SERIAL"
    )
    if explicit_serial:
        selected = next(
            (device for device in devices if device.serial == explicit_serial), None
        )
        if selected is None:
            raise RuntimeError(
                f"configured Android serial {explicit_serial!r} is not connected; "
                f"connected devices: {_device_summary(devices)}"
            )
        if selected.state != "device":
            raise RuntimeError(
                f"configured Android serial {explicit_serial!r} is {selected.state}, "
                "not ready"
            )
        if mode != "auto" and selected.kind != mode:
            raise RuntimeError(
                f"configured Android serial {explicit_serial!r} is {selected.kind}, "
                f"but {mode} mode was requested"
            )
        return selected

    ready = [device for device in devices if device.state == "device"]
    physical = [device for device in ready if not device.is_emulator]
    emulators = [device for device in ready if device.is_emulator]

    candidates = physical if mode in {"auto", "physical"} else emulators
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        serials = ", ".join(device.serial for device in candidates)
        raise RuntimeError(
            f"multiple ready Android {candidates[0].kind} devices found: {serials}; "
            "set BLEHUB_ANDROID_SERIAL to select one explicitly"
        )
    if mode == "physical":
        raise RuntimeError(
            "no ready physical Android device found; connected devices: "
            f"{_device_summary(devices)}"
        )

    if mode == "auto" and len(emulators) == 1:
        return emulators[0]
    if mode == "auto" and len(emulators) > 1:
        serials = ", ".join(device.serial for device in emulators)
        raise RuntimeError(
            f"multiple ready Android emulators found: {serials}; "
            "set BLEHUB_ANDROID_SERIAL to select one explicitly"
        )

    start_emulator()
    devices = _connected_devices(environment)
    emulators = [
        device for device in devices if device.is_emulator and device.state == "device"
    ]
    if len(emulators) != 1:
        raise RuntimeError(
            "expected exactly one ready emulator after startup; connected devices: "
            f"{_device_summary(devices)}"
        )
    return emulators[0]


def start_emulator() -> None:
    environment = toolchain_environment()
    running = _running_emulators(environment)
    if running:
        print(f"android_emulator={running[0]}")
        print("android_emulator_started=false")
        return
    if AVD_NAME not in _avd_names(environment):
        raise RuntimeError(
            f"missing AVD {AVD_NAME}; run `pixi run just android-avd-bootstrap`"
        )

    executable = android_executable()
    result = subprocess.run(
        [
            str(executable),
            f"--sdk={sdk_root()}",
            "emulator",
            "start",
            "--cold",
            AVD_NAME,
        ],
        env=environment,
        check=False,
    )
    running = _running_emulators(environment)
    if result.returncode != 0 or not running:
        raise RuntimeError(
            f"Android emulator start failed with exit code {result.returncode}"
        )
    print(f"android_emulator={running[0]}")
    print("android_emulator_started=true")


def stop_emulator() -> None:
    environment = toolchain_environment()
    if not _running_emulators(environment):
        print("android_emulator_stopped=false")
        return
    executable = android_executable()
    result = subprocess.run(
        [
            str(executable),
            f"--sdk={sdk_root()}",
            "emulator",
            "stop",
            AVD_NAME,
        ],
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Android emulator stop failed with exit code {result.returncode}"
        )
    print("android_emulator_stopped=true")


def install() -> None:
    executable = verify_android_cli()
    root = sdk_root()
    root.mkdir(parents=True, exist_ok=True)

    missing = [package for package in PACKAGES if package_problems(package, root)]
    if not missing:
        verify_packages()
        return

    command = [
        str(executable),
        f"--sdk={root}",
        "sdk",
        "install",
        *(package.specification for package in missing),
    ]
    result = subprocess.run(command, check=False)

    try:
        verify_packages()
    except RuntimeError as error:
        raise RuntimeError(
            f"Android CLI exited with code {result.returncode} and required packages "
            f"did not pass post-install verification: {error}"
        ) from error

    if result.returncode != 0:
        print(
            f"warning: Android CLI returned {result.returncode}, but all pinned packages "
            "passed post-install verification",
            file=sys.stderr,
        )


def report(*, require_complete: bool) -> None:
    configured = configured_sdk_root()
    root = configured.path
    executable = android_executable()
    returncode, version = _run_output([str(executable), "--version"])

    print(f"android_cli={executable}")
    print(f"android_cli_version_policy={ANDROID_CLI_VERSION_POLICY}")
    print(f"android_cli_version={version}")
    print(f"android_cli_version_exit_code={returncode}")
    print(f"android_sdk_root={root}")
    print(f"android_sdk_root_source={configured.source}")
    print(f"android_home={os.environ.get('ANDROID_HOME', '')}")
    print(f"android_sdk_root_environment={os.environ.get('ANDROID_SDK_ROOT', '')}")
    print(f"android_ndk_home={root / 'ndk' / '28.2.13676358'}")
    print(f"android_avd_home={root / 'avd'}")
    print(f"android_avd_name={AVD_NAME}")

    complete = True
    for package in PACKAGES:
        problems = package_problems(package, root)
        installed = not problems
        complete = complete and installed
        print(f"android_package_{package.package.replace('/', '_')}={package.version}")
        print(
            f"android_package_{package.package.replace('/', '_')}_installed="
            f"{str(installed).lower()}"
        )
        for problem in problems:
            print(f"android_package_problem={problem}")
    print(f"android_toolchain_complete={str(complete).lower()}")

    if require_complete:
        verify_android_cli()
        verify_packages()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install and verify BleHub's pinned Android SDK/NDK toolchain."
    )
    parser.add_argument(
        "command",
        choices=(
            "install",
            "verify",
            "report",
            "avd-create",
            "emulator-start",
            "emulator-stop",
            "device-select",
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "physical", "emulator"),
        default="auto",
        help="device preference for device-select",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "install":
            install()
            report(require_complete=True)
        elif args.command == "verify":
            report(require_complete=True)
        elif args.command == "report":
            report(require_complete=False)
        elif args.command == "avd-create":
            create_avd()
        elif args.command == "emulator-start":
            start_emulator()
        elif args.command == "emulator-stop":
            stop_emulator()
        else:
            selected = select_test_device(args.mode)
            print(f"android_test_device={selected.serial}")
            print(f"android_test_device_kind={selected.kind}")
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
