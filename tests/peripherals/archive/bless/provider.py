from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import tomllib
from qemu.qmp import QMPClient, QMPError

from tools.local_config import resolve_local_value, resolve_qemu_root

PROJECT_ROOT = Path(__file__).resolve().parents[4]
VM_SOURCE_ROOT = PROJECT_ROOT / "vm" / "archive" / "linux-peripheral"
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "peripherals" / "archive" / "bless"
SCENARIO_PATH = (
    PROJECT_ROOT
    / "tests"
    / "conformance"
    / "scenarios"
    / "archive"
    / "bless"
    / "qemu-basic-gatt-peripheral.json"
)
GUEST_SCENARIO_NAME = "basic-gatt-peripheral.json"
LOCK_PATH = VM_SOURCE_ROOT / "image.lock.toml"

WORK_ROOT = PROJECT_ROOT / ".work" / "vm" / "linux-peripheral"
DOWNLOAD_ROOT = WORK_ROOT / "downloads"
BASE_ROOT = WORK_ROOT / "base-image"
OVERLAY_ROOT = WORK_ROOT / "overlays"
PACKER_ROOT = VM_SOURCE_ROOT / "packer"
PACKER_TEMPLATE = PACKER_ROOT / "linux-peripheral.pkr.hcl"
PACKER_WORK_ROOT = WORK_ROOT / "packer"
PACKER_PLUGIN_ROOT = PACKER_WORK_ROOT / "plugins"
PACKER_CACHE_ROOT = PACKER_WORK_ROOT / "cache"
PACKER_OUTPUT_ROOT = BASE_ROOT / "packer-output"
QMP_ROOT = WORK_ROOT / "qmp"
RUN_ROOT = WORK_ROOT / "run"
LOG_ROOT = WORK_ROOT / "logs"
SSH_ROOT = WORK_ROOT / "ssh"
BUILD_IMAGE = BASE_ROOT / "provisioned.qcow2"
BUILD_MARKER = BASE_ROOT / "provisioned.json"
RUNTIME_IMAGE = OVERLAY_ROOT / "runtime.qcow2"
PID_FILE = RUN_ROOT / "qemu.pid"
QEMU_LOG = LOG_ROOT / "qemu.log"
SERIAL_LOG = LOG_ROOT / "serial.log"
PACKER_SERIAL_LOG = LOG_ROOT / "packer-serial.log"
SSH_PRIVATE_KEY = SSH_ROOT / "id_ed25519"
SSH_PUBLIC_KEY = SSH_ROOT / "id_ed25519.pub"
SSH_HOST = "127.0.0.1"
SSH_PORT = 22222
QMP_HOST = "127.0.0.1"
QMP_PORT = 4444
GUEST_USER = "blehub"
GUEST_FIXTURE_ROOT = "/opt/blehub/fixture"
GUEST_ENVIRONMENTS_ROOT = f"{GUEST_FIXTURE_ROOT}/environments"
GUEST_RELEASES_ROOT = f"{GUEST_FIXTURE_ROOT}/releases"
GUEST_CURRENT_ENVIRONMENT = f"{GUEST_FIXTURE_ROOT}/current-environment"
GUEST_CURRENT_RELEASE = f"{GUEST_FIXTURE_ROOT}/current"
GUEST_RUNTIME_ROOT = "/run/blehub-peripheral"
GUEST_CONTROL_REQUEST = f"{GUEST_RUNTIME_ROOT}/control-request.json"
GUEST_CONTROL_RESPONSE = f"{GUEST_RUNTIME_ROOT}/control-response.json"
PACKER_VERSION = "1.16.0"


def load_lock() -> dict[str, Any]:
    if sys.platform == "darwin":
        raise RuntimeError(
            "the Linux BLESS VM is maintained on Windows/Linux; use Android or nRF52840 DK on macOS"
        )
    with LOCK_PATH.open("rb") as stream:
        document = tomllib.load(stream)
    if document.get("schema_version") != 1:
        raise RuntimeError(f"unsupported VM image lock schema in {LOCK_PATH}")
    return document


def ensure_work_directories() -> None:
    for path in (
        DOWNLOAD_ROOT,
        BASE_ROOT,
        OVERLAY_ROOT,
        PACKER_PLUGIN_ROOT,
        PACKER_CACHE_ROOT,
        QMP_ROOT,
        RUN_ROOT,
        LOG_ROOT,
        SSH_ROOT,
    ):
        path.mkdir(parents=True, exist_ok=True)


def hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_hash(path: Path, algorithm: str, expected: str) -> None:
    actual = hash_file(path, algorithm)
    if actual != expected:
        raise RuntimeError(
            f"checksum mismatch for {path}: expected {expected}, got {actual}"
        )


def download(url: str, destination: Path, algorithm: str, expected: str) -> Path:
    if destination.is_file():
        verify_hash(destination, algorithm, expected)
        print(f"asset_cached={destination}")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    print(f"asset_download_url={url}")
    print(f"asset_download_path={destination}")
    request = urllib.request.Request(url, headers={"User-Agent": "BleHub VM bootstrap"})
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        temporary.open("wb") as stream,
    ):
        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header else None
        written = 0
        next_report = 64 * 1024 * 1024
        while chunk := response.read(1024 * 1024):
            stream.write(chunk)
            written += len(chunk)
            if written >= next_report:
                if total:
                    print(f"asset_download_progress={written}/{total}")
                else:
                    print(f"asset_download_progress={written}")
                next_report += 64 * 1024 * 1024
    verify_hash(temporary, algorithm, expected)
    temporary.replace(destination)
    print(f"asset_verified={destination}")
    return destination


def qemu_install_root(lock: dict[str, Any]) -> Path:
    configured = resolve_qemu_root("BLEHUB_QEMU_ROOT")
    return configured.path / lock["qemu"]["version"]


def qemu_system_binary(lock: dict[str, Any]) -> str:
    return str(lock["qemu"].get("system_binary", "qemu-system-x86_64"))


def qemu_executable(lock: dict[str, Any], name: str) -> Path:
    if os.name != "nt":
        resolved = shutil.which(name)
        if resolved is not None:
            return Path(resolved).resolve()
    suffix = ".exe" if os.name == "nt" else ""
    return qemu_install_root(lock) / f"{name}{suffix}"


def expected_qemu_version(lock: dict[str, Any]) -> str:
    return str(lock["qemu"]["version"])


def qemu_version(path: Path) -> str:
    result = subprocess.run(
        [str(path), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"QEMU version check failed for {path}: {result.stderr.strip()}"
        )
    return result.stdout.splitlines()[0].strip()


def packer_executable() -> str:
    executable = shutil.which("packer")
    if executable is None:
        raise RuntimeError(
            "Packer is unavailable; run this command through `pixi run just`"
        )
    result = subprocess.run(
        [executable, "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or f"Packer v{PACKER_VERSION}" not in result.stdout:
        raise RuntimeError(f"unexpected Packer version: {result.stdout.strip()!r}")
    return executable


def packer_environment(lock: dict[str, Any]) -> dict[str, str]:
    environment = os.environ.copy()
    qemu_root = str(qemu_executable(lock, qemu_system_binary(lock)).parent)
    environment["PATH"] = qemu_root + os.pathsep + environment.get("PATH", "")
    environment["PACKER_PLUGIN_PATH"] = str(PACKER_PLUGIN_ROOT)
    environment["PACKER_CACHE_DIR"] = str(PACKER_CACHE_ROOT)
    environment["SDL_STDIO_REDIRECT"] = "0"
    return environment


def run_packer(lock: dict[str, Any], *arguments: str, timeout: int = 3600) -> None:
    result = subprocess.run(
        [packer_executable(), *arguments],
        cwd=PROJECT_ROOT,
        env=packer_environment(lock),
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Packer failed with exit code {result.returncode}: {arguments}"
        )


def initialize_packer(lock: dict[str, Any]) -> None:
    PACKER_PLUGIN_ROOT.mkdir(parents=True, exist_ok=True)
    PACKER_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    run_packer(lock, "init", str(PACKER_TEMPLATE), timeout=600)


def packer_variable_arguments(
    lock: dict[str, Any], qemu: Path, base_image: Path, fingerprint: str
) -> list[str]:
    pixi = lock["pixi"]
    arguments = [
        f"-var=base_image={base_image.as_posix()}",
        f"-var=base_image_sha512={lock['guest_image']['sha512']}",
        f"-var=instance_id=blehub-linux-peripheral-{fingerprint[:16]}",
        f"-var=image_fingerprint={fingerprint}",
        f"-var=output_directory={PACKER_OUTPUT_ROOT.as_posix()}",
        f"-var=pixi_archive={(DOWNLOAD_ROOT / pixi['filename']).as_posix()}",
        f"-var=pixi_sha256={pixi['sha256']}",
        f"-var=pixi_version={pixi['version']}",
        f"-var=qemu_binary={qemu.as_posix()}",
        f"-var=serial_log={PACKER_SERIAL_LOG.as_posix()}",
        f"-var=ssh_private_key_file={SSH_PRIVATE_KEY.as_posix()}",
        f"-var=ssh_public_key_file={SSH_PUBLIC_KEY.as_posix()}",
    ]
    firmware = qemu_firmware_code(lock, qemu)
    if firmware is not None:
        arguments.append(f"-var=firmware_code={firmware.as_posix()}")
    return arguments


def qemu_firmware_code(lock: dict[str, Any], qemu: Path) -> Path | None:
    filename = lock["qemu"].get("firmware_code")
    if filename is None:
        return None
    firmware = qemu.parent.parent / "share" / "qemu" / str(filename)
    if not firmware.is_file():
        raise RuntimeError(f"QEMU firmware is missing: {firmware}")
    return firmware


def verify_qemu(lock: dict[str, Any]) -> Path:
    executable = qemu_executable(lock, qemu_system_binary(lock))
    image_tool = qemu_executable(lock, "qemu-img")
    if not executable.is_file() or not image_tool.is_file():
        instruction = "run `pixi run just peripheral vm-bootstrap`"
        raise RuntimeError(f"pinned QEMU is not installed; {instruction}")
    version = expected_qemu_version(lock)
    output = qemu_version(executable)
    if version not in output:
        raise RuntimeError(f"unexpected QEMU version: {output!r}; expected {version}")
    qemu_firmware_code(lock, executable)
    return executable


def install_qemu(lock: dict[str, Any]) -> Path:
    if os.name != "nt":
        raise RuntimeError(
            "the current pinned QEMU bootstrap supports Windows hosts; macOS uses Android or nRF52840 DK"
        )
    try:
        return verify_qemu(lock)
    except RuntimeError:
        pass

    qemu = lock["qemu"]
    configured = resolve_qemu_root("BLEHUB_QEMU_ROOT")
    installer = download(
        qemu["installer_url"],
        configured.path / "downloads" / qemu["installer_filename"],
        "sha512",
        qemu["installer_sha512"],
    )
    archive_tool = shutil.which("7z") or shutil.which("7zz")
    if archive_tool is None:
        raise RuntimeError(
            "7-Zip is unavailable; run bootstrap through the project Pixi environment"
        )
    install_root = qemu_install_root(lock)
    if install_root.exists():
        shutil.rmtree(install_root)
    install_root.mkdir(parents=True)
    print(f"qemu_install_root={install_root}")
    print("qemu_install_mode=portable-nsis-extraction")
    result = subprocess.run(
        [str(archive_tool), "x", "-y", f"-o{install_root}", str(installer)],
        check=False,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(f"QEMU extraction failed with exit code {result.returncode}")
    shutil.rmtree(install_root / "$PLUGINSDIR", ignore_errors=True)
    return verify_qemu(lock)


def guest_image_path(lock: dict[str, Any]) -> Path:
    return DOWNLOAD_ROOT / lock["guest_image"]["filename"]


def verify_guest_image(lock: dict[str, Any]) -> Path:
    image = guest_image_path(lock)
    if not image.is_file():
        raise RuntimeError(
            "pinned Debian guest image is not downloaded; run "
            "`pixi run just peripheral vm-bootstrap`"
        )
    verify_hash(image, "sha512", lock["guest_image"]["sha512"])
    return image


def ensure_ssh_key() -> None:
    if SSH_PRIVATE_KEY.is_file() and SSH_PUBLIC_KEY.is_file():
        return
    system_ssh_keygen = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
        "System32/OpenSSH/ssh-keygen.exe"
    )
    executable = (
        system_ssh_keygen
        if system_ssh_keygen.is_file()
        else Path(shutil.which("ssh-keygen") or "")
    )
    if not executable.is_file():
        raise RuntimeError("ssh-keygen is required to create the VM access key")
    SSH_PRIVATE_KEY.unlink(missing_ok=True)
    SSH_PUBLIC_KEY.unlink(missing_ok=True)
    subprocess.run(
        [
            str(executable),
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "blehub-linux-peripheral",
            "-f",
            str(SSH_PRIVATE_KEY),
        ],
        check=True,
        timeout=30,
    )


def bootstrap() -> None:
    ensure_work_directories()
    lock = load_lock()
    qemu = install_qemu(lock)
    guest = lock["guest_image"]
    image = download(
        guest["url"],
        guest_image_path(lock),
        "sha512",
        guest["sha512"],
    )
    pixi = lock["pixi"]
    pixi_asset = download(
        pixi["url"],
        DOWNLOAD_ROOT / pixi["filename"],
        "sha256",
        pixi["sha256"],
    )

    ensure_ssh_key()
    initialize_packer(lock)
    print(f"qemu_executable={qemu}")
    print(f"qemu_version_output={qemu_version(qemu)}")
    print(f"guest_image={image}")
    print(f"guest_image_build={guest['build']}")
    print(f"ssh_public_key={SSH_PUBLIC_KEY}")
    print(f"packer_executable={packer_executable()}")
    print(f"packer_plugin_root={PACKER_PLUGIN_ROOT}")
    print(f"pixi_asset={pixi_asset}")


def fingerprint_files(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def image_fingerprint() -> str:
    return fingerprint_files(
        (
            LOCK_PATH,
            PACKER_TEMPLATE,
            PACKER_ROOT / "cloud-init-meta-data.pkrtpl.hcl",
            PACKER_ROOT / "cloud-init-user-data.pkrtpl.hcl",
            VM_SOURCE_ROOT / "provision" / "provision.sh",
            VM_SOURCE_ROOT / "provision" / "blehub-peripheral.service",
            SSH_PUBLIC_KEY,
        )
    )


def fixture_environment_fingerprint() -> str:
    return fingerprint_files((FIXTURE_ROOT / "pixi.toml", FIXTURE_ROOT / "pixi.lock"))


def fixture_fingerprint() -> str:
    return fingerprint_files((FIXTURE_ROOT / "bless_fixture.py", SCENARIO_PATH))


def run_checked(command: list[str], *, timeout: int = 120) -> None:
    result = subprocess.run(command, check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: {command}"
        )


def qemu_img(lock: dict[str, Any], *arguments: str) -> None:
    run_checked([str(qemu_executable(lock, "qemu-img")), *arguments], timeout=300)


def create_overlay(lock: dict[str, Any], backing: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    qemu_img(
        lock,
        "create",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(backing),
        str(destination),
    )
    qemu_img(lock, "resize", str(destination), "12G")


def qemu_command(lock: dict[str, Any], image: Path, accelerator: str) -> list[str]:
    qemu = verify_qemu(lock)
    command = [
        str(qemu),
        "-name",
        "blehub-linux-peripheral",
        "-machine",
        str(lock["qemu"].get("machine_type", "q35")),
        "-accel",
        accelerator,
        "-smp",
        "2",
        "-m",
        "2048",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        f"file:{SERIAL_LOG.as_posix()}",
        "-pidfile",
        str(PID_FILE),
        "-qmp",
        f"tcp:{QMP_HOST}:{QMP_PORT},server=on,wait=off",
        "-drive",
        f"file={image.as_posix()},if=virtio,format=qcow2,cache=writeback",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:{SSH_HOST}:{SSH_PORT}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-device",
        "qemu-xhci,id=xhci",
    ]
    cpu_model = lock["qemu"].get("cpu_model")
    if cpu_model is not None:
        command.extend(("-cpu", str(cpu_model)))
    firmware = qemu_firmware_code(lock, qemu)
    if firmware is not None:
        command.extend(("-bios", str(firmware)))
    return command


async def qmp_execute_async(
    command: str, arguments: dict[str, Any] | None = None
) -> Any:
    client = QMPClient("blehub-linux-peripheral")
    await client.connect((QMP_HOST, QMP_PORT))
    try:
        return await client.execute(command, arguments)
    finally:
        await client.disconnect()


def qmp_execute(command: str, arguments: dict[str, Any] | None = None) -> Any:
    return asyncio.run(
        asyncio.wait_for(qmp_execute_async(command, arguments), timeout=5)
    )


def qmp_available() -> bool:
    try:
        qmp_execute("query-status")
    except (EOFError, OSError, QMPError, TimeoutError):
        return False
    return True


def wait_for_qmp(timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if qmp_available():
            return
        time.sleep(0.5)
    detail = (
        QEMU_LOG.read_text(encoding="utf-8", errors="replace")
        if QEMU_LOG.exists()
        else ""
    )
    raise RuntimeError(f"QEMU did not expose QMP within {timeout}s\n{detail[-4000:]}")


def start_qemu(lock: dict[str, Any], image: Path, accelerator: str) -> None:
    if qmp_available():
        raise RuntimeError("the Linux Peripheral VM is already running")
    ensure_work_directories()
    PID_FILE.unlink(missing_ok=True)
    QEMU_LOG.unlink(missing_ok=True)
    SERIAL_LOG.unlink(missing_ok=True)
    command = qemu_command(lock, image, accelerator)
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    with QEMU_LOG.open("ab") as log:
        subprocess.Popen(
            command,
            cwd=verify_qemu(lock).parent,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    wait_for_qmp()
    print(f"qemu_accelerator={accelerator}")
    print(f"qemu_qmp=tcp:{QMP_HOST}:{QMP_PORT}")
    print(f"qemu_serial_log={SERIAL_LOG}")


def system_ssh_executable() -> Path:
    if os.name == "nt":
        candidate = Path(os.environ.get("WINDIR", r"C:\Windows")) / (
            "System32/OpenSSH/ssh.exe"
        )
        if candidate.is_file():
            return candidate
    resolved = shutil.which("ssh")
    if resolved is None:
        raise RuntimeError("OpenSSH ssh is required for VM orchestration")
    return Path(resolved)


def ssh_command(remote_command: str) -> list[str]:
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    return [
        str(system_ssh_executable()),
        "-i",
        str(SSH_PRIVATE_KEY),
        "-p",
        str(SSH_PORT),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={null_device}",
        f"{GUEST_USER}@{SSH_HOST}",
        remote_command,
    ]


def ssh(
    remote_command: str,
    *,
    check: bool = True,
    capture: bool = False,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ssh_command(remote_command),
        check=False,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = "\n".join(
            value.strip()
            for value in (result.stdout, result.stderr)
            if value and value.strip()
        )
        raise RuntimeError(
            f"guest SSH command failed with exit code {result.returncode}: "
            f"{remote_command}\n{detail}"
        )
    return result


def system_scp_executable() -> Path:
    ssh_executable = system_ssh_executable()
    sibling = ssh_executable.with_name("scp.exe" if os.name == "nt" else "scp")
    if sibling.is_file():
        return sibling
    resolved = shutil.which("scp")
    if resolved is None:
        raise RuntimeError("OpenSSH scp is required for fixture deployment")
    return Path(resolved)


def scp(local_paths: tuple[Path, ...], remote_directory: str) -> None:
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    command = [
        str(system_scp_executable()),
        "-i",
        str(SSH_PRIVATE_KEY),
        "-P",
        str(SSH_PORT),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={null_device}",
        *(str(path) for path in local_paths),
        f"{GUEST_USER}@{SSH_HOST}:{remote_directory}/",
    ]
    run_checked(command, timeout=300)


def scp_verified(local_paths: tuple[Path, ...], remote_directory: str) -> None:
    ensure_work_directories()
    manifest = RUN_ROOT / f"scp-{secrets.token_hex(8)}.sha256"
    manifest.write_text(
        "".join(f"{hash_file(path, 'sha256')}  {path.name}\n" for path in local_paths),
        encoding="utf-8",
        newline="\n",
    )
    try:
        scp((*local_paths, manifest), remote_directory)
        ssh(
            f"cd {shlex.quote(remote_directory)} && "
            f"sha256sum --check --strict {shlex.quote(manifest.name)}",
            timeout=60,
        )
    finally:
        manifest.unlink(missing_ok=True)


def guest_json(path: str, *, timeout: int = 30) -> dict[str, Any] | None:
    result = ssh(
        f"sudo cat {shlex.quote(path)}",
        check=False,
        capture=True,
        timeout=timeout,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise TypeError(f"guest marker {path} must contain a JSON object")
    return document


def write_guest_json(path: str, document: dict[str, Any]) -> None:
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
    ssh(
        f"printf '%s\\n' {shlex.quote(payload)} > {shlex.quote(path)}",
        timeout=30,
    )


def write_guest_runtime_json(path: str, document: dict[str, Any]) -> None:
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
    temporary = f"{path}.{secrets.token_hex(8)}.tmp"
    ssh(
        f"printf '%s\\n' {shlex.quote(payload)} | "
        f"sudo tee {shlex.quote(temporary)} >/dev/null && "
        f"sudo chmod 0600 {shlex.quote(temporary)} && "
        f"sudo mv -f {shlex.quote(temporary)} {shlex.quote(path)}",
        timeout=30,
    )


def activate_guest_link(name: str, target: str, fingerprint: str) -> None:
    temporary = f".{name}-{fingerprint}"
    command = " && ".join(
        (
            f"cd {shlex.quote(GUEST_FIXTURE_ROOT)}",
            f"sudo rm -f {shlex.quote(temporary)}",
            f"sudo ln -s {shlex.quote(target)} {shlex.quote(temporary)}",
            f"sudo mv -Tf {shlex.quote(temporary)} {shlex.quote(name)}",
        )
    )
    ssh(command, timeout=30)


def guest_link_target(name: str) -> str | None:
    result = ssh(
        f"readlink {shlex.quote(f'{GUEST_FIXTURE_ROOT}/{name}')}",
        check=False,
        capture=True,
        timeout=30,
    )
    target = result.stdout.strip()
    return target if result.returncode == 0 and target else None


def guest_fingerprint(marker_path: str, key: str) -> str | None:
    marker = guest_json(marker_path)
    value = marker.get(key) if marker is not None else None
    return value if isinstance(value, str) else None


def fingerprint_status(actual: str | None, expected: str) -> str:
    if actual is None:
        return "missing"
    if actual != expected:
        return "stale"
    return actual


def report_guest_fingerprints() -> None:
    expected_image = image_fingerprint()
    actual_image = guest_fingerprint(
        "/var/lib/blehub/provisioned.json", "image_fingerprint"
    )
    print(f"image_fingerprint={fingerprint_status(actual_image, expected_image)}")

    expected_environment = fixture_environment_fingerprint()
    actual_environment = guest_fingerprint(
        f"{GUEST_CURRENT_ENVIRONMENT}/blehub-environment.json",
        "fixture_environment_fingerprint",
    )
    print(
        "fixture_environment_fingerprint="
        f"{fingerprint_status(actual_environment, expected_environment)}"
    )
    if actual_environment is not None and actual_environment != expected_environment:
        print(f"fixture_environment_active_fingerprint={actual_environment}")

    expected_fixture = fixture_fingerprint()
    actual_fixture = guest_fingerprint(
        f"{GUEST_CURRENT_RELEASE}/blehub-fixture.json",
        "fixture_source_fingerprint",
    )
    print(
        "fixture_source_fingerprint="
        f"{fingerprint_status(actual_fixture, expected_fixture)}"
    )
    if actual_fixture is not None and actual_fixture != expected_fixture:
        print(f"fixture_source_active_fingerprint={actual_fixture}")


def wait_for_ssh(timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = ssh("true", check=False, capture=True, timeout=10)
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            print("guest_ssh=ready")
            return
        time.sleep(3)
    raise RuntimeError(f"guest SSH did not become ready within {timeout}s")


def stop_running_vm(timeout: int = 120) -> None:
    if not qmp_available():
        PID_FILE.unlink(missing_ok=True)
        print("vm_running=false")
        return
    try:
        qmp_execute("system_powerdown")
    except (OSError, QMPError):
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not qmp_available():
            PID_FILE.unlink(missing_ok=True)
            print("vm_stopped=true")
            return
        time.sleep(2)
    qmp_execute("quit")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and qmp_available():
        time.sleep(0.5)
    PID_FILE.unlink(missing_ok=True)
    if qmp_available():
        raise RuntimeError("QEMU did not stop after ACPI shutdown and QMP quit")
    print("vm_stopped=true")


def build() -> None:
    ensure_work_directories()
    lock = load_lock()
    qemu = verify_qemu(lock)
    base_image = verify_guest_image(lock)
    ensure_ssh_key()
    fingerprint = image_fingerprint()
    if BUILD_MARKER.is_file() and BUILD_IMAGE.is_file():
        marker = json.loads(BUILD_MARKER.read_text(encoding="utf-8"))
        if marker.get("image_fingerprint") == fingerprint:
            print(f"vm_build_image={BUILD_IMAGE}")
            print("vm_build_cached=true")
            return
    if qmp_available():
        raise RuntimeError("stop the running VM before rebuilding its base image")

    shutil.rmtree(PACKER_OUTPUT_ROOT, ignore_errors=True)
    PACKER_SERIAL_LOG.unlink(missing_ok=True)
    RUNTIME_IMAGE.unlink(missing_ok=True)
    pixi = lock["pixi"]
    variables = packer_variable_arguments(lock, qemu, base_image, fingerprint)
    run_packer(
        lock,
        "validate",
        *variables,
        str(PACKER_TEMPLATE),
        timeout=300,
    )
    run_packer(
        lock,
        "build",
        "-force",
        *variables,
        str(PACKER_TEMPLATE),
        timeout=3600,
    )
    candidate = PACKER_OUTPUT_ROOT / "provisioned.qcow2"
    if not candidate.is_file():
        raise RuntimeError(f"Packer did not produce {candidate}")

    BUILD_IMAGE.unlink(missing_ok=True)
    candidate.replace(BUILD_IMAGE)
    shutil.rmtree(PACKER_OUTPUT_ROOT, ignore_errors=True)
    BUILD_MARKER.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "image_fingerprint": fingerprint,
                "guest_image_build": lock["guest_image"]["build"],
                "qemu_version": expected_qemu_version(lock),
                "packer_version": PACKER_VERSION,
                "packer_qemu_plugin_version": "1.1.6",
                "pixi_version": pixi["version"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"vm_build_image={BUILD_IMAGE}")
    print("vm_build_cached=false")


def select_accelerator() -> str:
    requested = os.environ.get("BLEHUB_VM_ACCEL", "auto").strip().lower()
    supported = {"tcg", "whpx"} if os.name == "nt" else {"tcg", "hvf"}
    if requested in supported:
        print(f"qemu_accelerator_selection=explicit-{requested}")
        return requested
    if requested != "auto":
        values = ", ".join(f"`{value}`" for value in sorted(supported))
        raise RuntimeError(f"BLEHUB_VM_ACCEL must be `auto` or one of {values}")
    if sys.platform == "darwin":
        accelerator = str(load_lock()["qemu"].get("accelerator", "hvf"))
        print(f"qemu_accelerator_selection=auto-{accelerator}-macos")
        return accelerator
    if os.name != "nt":
        print("qemu_accelerator_selection=auto-tcg-non-windows")
        return "tcg"
    try:
        enabled = int(whpx_state()["InstallState"]) == 1
    except (OSError, RuntimeError, ValueError):
        print("qemu_accelerator_selection=auto-tcg-whpx-status-unavailable")
        return "tcg"
    accelerator = "whpx" if enabled else "tcg"
    print(f"qemu_accelerator_selection=auto-{accelerator}")
    return accelerator


def start() -> None:
    ensure_work_directories()
    lock = load_lock()
    verify_qemu(lock)
    if not BUILD_IMAGE.is_file() or not BUILD_MARKER.is_file():
        raise RuntimeError(
            "the provisioned VM image is missing; run "
            "`pixi run just peripheral vm-build`"
        )
    marker = json.loads(BUILD_MARKER.read_text(encoding="utf-8"))
    if marker.get("image_fingerprint") != image_fingerprint():
        raise RuntimeError(
            "VM provisioning inputs changed; rerun `pixi run just peripheral vm-build`"
        )
    if not RUNTIME_IMAGE.is_file():
        create_overlay(lock, BUILD_IMAGE, RUNTIME_IMAGE)
    start_qemu(lock, RUNTIME_IMAGE, select_accelerator())
    wait_for_ssh(timeout=600)
    print(f"vm_image={RUNTIME_IMAGE}")
    print(f"guest_ssh={GUEST_USER}@{SSH_HOST}:{SSH_PORT}")


def status() -> None:
    lock = load_lock()
    executable = qemu_executable(lock, qemu_system_binary(lock))
    if os.name == "nt":
        configured = resolve_qemu_root("BLEHUB_QEMU_ROOT")
        print(f"qemu_root={configured.path}")
        print(f"qemu_root_source={configured.source}")
    else:
        print(f"qemu_root={executable.parent}")
        print("qemu_root_source=PATH")
    print(f"qemu_executable={executable}")
    print(f"qemu_installed={str(executable.is_file()).lower()}")
    if executable.is_file():
        print(f"qemu_version_output={qemu_version(executable)}")
    image = guest_image_path(lock)
    print(f"guest_image={image}")
    print(f"guest_image_downloaded={str(image.is_file()).lower()}")
    print(f"vm_build_image={BUILD_IMAGE}")
    print(f"vm_built={str(BUILD_IMAGE.is_file() and BUILD_MARKER.is_file()).lower()}")
    print(f"vm_runtime_image={RUNTIME_IMAGE}")
    print(f"vm_runtime_created={str(RUNTIME_IMAGE.is_file()).lower()}")
    running = qmp_available()
    print(f"vm_running={str(running).lower()}")
    if running:
        print(
            f"vm_qmp_status={json.dumps(qmp_execute('query-status'), sort_keys=True)}"
        )
        result = ssh("uname -srmo", check=False, capture=True, timeout=15)
        if result.returncode == 0:
            print(f"guest_uname={result.stdout.strip()}")
            print("guest_ssh=ready")
            report_guest_fingerprints()
        else:
            print("guest_ssh=unavailable")


def doctor() -> None:
    lock = load_lock()
    verify_qemu(lock)
    if not BUILD_IMAGE.is_file() or not BUILD_MARKER.is_file():
        raise RuntimeError("the VM has not been built")
    if not qmp_available():
        raise RuntimeError("the VM is not running; run `peripheral vm-start`")
    wait_for_ssh(timeout=30)
    report_guest_fingerprints()
    command = """set -eu
printf 'guest_os='; . /etc/os-release; printf '%s %s\\n' "$ID" "$VERSION_ID"
printf 'guest_kernel='; uname -r
printf 'cloud_init_status='; cloud-init status
printf 'dbus_service='; systemctl is-active dbus.service
printf 'pixi_version='; /opt/blehub/pixi/bin/pixi --version
if test -x /opt/blehub/fixture/current-environment/.pixi/envs/default/bin/python; then
    printf 'bless_version='
    /opt/blehub/fixture/current-environment/.pixi/envs/default/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("bless"))'
else
    echo 'bless_version=missing'
fi
if test -f /opt/blehub/fixture/current/basic-gatt-peripheral.json; then
    printf 'fixture_profile='
    python3 -c 'import json; print(json.load(open("/opt/blehub/fixture/current/basic-gatt-peripheral.json"))["profile_id"])'
else
    echo 'fixture_profile=missing'
fi
bluetooth_state="$(systemctl is-active bluetooth.service || true)"
printf 'bluetooth_service=%s\\n' "$bluetooth_state"
controller_list=""
if test "$bluetooth_state" = active; then
    controller_list="$(timeout 10 bluetoothctl list 2>/dev/null || true)"
fi
controller_count="$(printf '%s\\n' "$controller_list" | grep -c '^Controller ' || true)"
printf 'bluetooth_controllers=%s\\n' "$controller_count"
printf '%s\\n' "$controller_list"
if test "$controller_count" -eq 1; then
    rfkill_state="$(/usr/sbin/rfkill list bluetooth)"
    printf 'rfkill_state=%s\\n' "$(printf '%s' "$rfkill_state" | tr '\\n' ';')"
    ! printf '%s\\n' "$rfkill_state" | grep -q 'blocked: yes'
    adapter_path="$(busctl tree org.bluez --list | grep -E '^/org/bluez/hci[0-9]+$')"
    test "$(printf '%s\\n' "$adapter_path" | grep -c '^/org/bluez/hci')" -eq 1
    printf 'bluez_adapter_path=%s\\n' "$adapter_path"
    busctl introspect org.bluez "$adapter_path" org.bluez.GattManager1 --no-pager >/dev/null
    echo 'bluez_gatt_manager=true'
    busctl introspect org.bluez "$adapter_path" org.bluez.LEAdvertisingManager1 --no-pager >/dev/null
    echo 'bluez_le_advertising_manager=true'
    printf 'bluez_supported_advertising_instances='
    busctl get-property org.bluez "$adapter_path" org.bluez.LEAdvertisingManager1 SupportedInstances
fi
"""
    ssh(command, timeout=120)
    print("doctor_result=pass")
    print("doctor_note=USB passthrough is optional until a controller is configured")


def logs() -> None:
    print(f"qemu_log={QEMU_LOG}")
    print(f"serial_log={SERIAL_LOG}")
    print(f"packer_serial_log={PACKER_SERIAL_LOG}")
    if QEMU_LOG.is_file():
        print("--- qemu.log ---")
        print(QEMU_LOG.read_text(encoding="utf-8", errors="replace"))
    if SERIAL_LOG.is_file():
        print("--- serial.log ---")
        print(SERIAL_LOG.read_text(encoding="utf-8", errors="replace"))
    if PACKER_SERIAL_LOG.is_file():
        print("--- packer-serial.log ---")
        print(PACKER_SERIAL_LOG.read_text(encoding="utf-8", errors="replace"))
    if qmp_available():
        print("--- guest cloud-init ---")
        ssh(
            "sudo journalctl -u cloud-init.service -u cloud-final.service "
            "--no-pager -n 200",
            check=False,
            timeout=60,
        )
        print("--- guest peripheral service ---")
        ssh(
            "sudo journalctl -u blehub-peripheral.service --no-pager -n 200",
            check=False,
            timeout=60,
        )


def reset() -> None:
    stop_running_vm()
    RUNTIME_IMAGE.unlink(missing_ok=True)
    print(f"vm_runtime_removed={RUNTIME_IMAGE}")


def clean() -> None:
    stop_running_vm()
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    print(f"vm_work_root_removed={WORK_ROOT}")


def whpx_state() -> dict[str, Any]:
    return powershell_json(
        "Get-CimInstance Win32_OptionalFeature -Filter "
        "\"Name='HypervisorPlatform'\" | "
        "Select-Object Name,InstallState,Status | ConvertTo-Json -Compress"
    )


def whpx_status() -> None:
    state = whpx_state()
    install_state = int(state["InstallState"])
    print(f"whpx_feature={state['Name']}")
    print(f"whpx_install_state={install_state}")
    print(f"whpx_enabled={str(install_state == 1).lower()}")
    print("whpx_qemu_supported=true")


def whpx_enable() -> None:
    if int(whpx_state()["InstallState"]) == 1:
        print("whpx_enable=already-enabled")
        return
    run_elevated_script(
        "$ErrorActionPreference = 'Stop'\n"
        "Enable-WindowsOptionalFeature -Online -FeatureName "
        "HypervisorPlatform -All -NoRestart\n"
    )
    whpx_status()
    print("whpx_enable=requested")
    print("whpx_reboot_required=true")


def powershell_json(script: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PowerShell device query failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run_elevated_script(script: str) -> None:
    if os.name != "nt":
        raise RuntimeError("elevated Windows operations are only available on Windows")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    command = (
        "$process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait "
        "-PassThru -ArgumentList @('-NoProfile','-NonInteractive',"
        "'-ExecutionPolicy','Bypass','-EncodedCommand',"
        f"'{encoded}'); exit $process.ExitCode"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "UAC was cancelled"
        raise RuntimeError(f"elevated operation failed: {detail}")


def peripheral_usb_config() -> dict[str, str] | None:
    values = {
        key: resolve_local_value(
            "peripheral_usb", key, environment_variable, None
        ).value
        for key, environment_variable in (
            ("vendor_id", "BLEHUB_PERIPHERAL_USB_VENDOR_ID"),
            ("product_id", "BLEHUB_PERIPHERAL_USB_PRODUCT_ID"),
            ("serial", "BLEHUB_PERIPHERAL_USB_SERIAL"),
            ("qmp_id", "BLEHUB_PERIPHERAL_USB_QMP_ID"),
            ("bus", "BLEHUB_PERIPHERAL_USB_BUS"),
        )
    }
    if values["vendor_id"] is None and values["product_id"] is None:
        return None
    if values["vendor_id"] is None or values["product_id"] is None:
        raise RuntimeError(
            "peripheral USB configuration requires both vendor_id and product_id"
        )
    for key in ("vendor_id", "product_id"):
        value = values[key]
        if (
            value is None
            or len(value) != 4
            or any(c not in "0123456789abcdefABCDEF" for c in value)
        ):
            raise RuntimeError(
                f"peripheral_usb.{key} must be exactly four hexadecimal digits"
            )
    qmp_id = values["qmp_id"] or "ble-controller"
    bus = values["bus"] or "xhci.0"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", qmp_id):
        raise RuntimeError("peripheral_usb.qmp_id contains unsupported characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", bus):
        raise RuntimeError("peripheral_usb.bus contains unsupported characters")
    result = {
        "vendor_id": values["vendor_id"].lower(),
        "product_id": values["product_id"].lower(),
        "qmp_id": qmp_id,
        "bus": bus,
    }
    if values["serial"] is not None:
        result["serial"] = values["serial"]
    return result


def require_peripheral_usb_config() -> dict[str, str]:
    config = peripheral_usb_config()
    if config is None:
        raise RuntimeError(
            "no USB peripheral controller configured; nRF52840 Dongle is pending"
        )
    return config


def qmp_usb_attached() -> bool | None:
    config = peripheral_usb_config()
    if config is None or not qmp_available():
        return None
    try:
        return bool(
            qmp_execute(
                "qom-get",
                {
                    "path": f"/machine/peripheral/{config['qmp_id']}",
                    "property": "attached",
                },
            )
        )
    except QMPError:
        return None


def print_usb_status() -> None:
    config = peripheral_usb_config()
    print(f"usb_controller_configured={str(config is not None).lower()}")
    if config is not None:
        for key, value in config.items():
            print(f"usb_controller_{key}={value}")
    attached = qmp_usb_attached()
    if attached is None:
        value = "absent" if qmp_available() and config is not None else "unavailable"
    else:
        value = str(attached).lower()
    print(f"qemu_usb_attached={value}")


def usb_status() -> None:
    print_usb_status()


def delete_qmp_usb() -> None:
    config = peripheral_usb_config()
    attached = qmp_usb_attached()
    if config is None or attached is None:
        return
    qmp_execute("device_del", {"id": config["qmp_id"]})
    deadline = time.monotonic() + (30 if sys.platform == "darwin" else 15)
    while time.monotonic() < deadline:
        if qmp_usb_attached() is None:
            print("qemu_usb_detached=true")
            return
        time.sleep(0.5)
    raise RuntimeError("QEMU did not remove the configured usb-host device")


def wait_for_guest_controller(timeout: int = 30) -> None:
    controller_ready = ssh(
        f"for attempt in $(seq 1 {timeout}); do "
        "controller_count=$(timeout 5 bluetoothctl list 2>/dev/null | grep -c '^Controller ' || true); "
        'test "$controller_count" -eq 1 && exit 0; '
        "sleep 1; "
        "done; exit 1",
        check=False,
        capture=True,
        timeout=timeout + 15,
    )
    if controller_ready.returncode != 0:
        raise RuntimeError(
            f"BlueZ did not enumerate exactly one controller within {timeout} seconds"
        )


def usb_attach() -> None:
    config = require_peripheral_usb_config()
    if not qmp_available():
        raise RuntimeError("the VM must be running before USB attach")
    delete_qmp_usb()
    arguments: dict[str, Any] = {
        "driver": "usb-host",
        "id": config["qmp_id"],
        "vendorid": int(config["vendor_id"], 16),
        "productid": int(config["product_id"], 16),
        "bus": config["bus"],
    }
    if "serial" in config:
        arguments["serial"] = config["serial"]
    qmp_execute("device_add", arguments)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if qmp_usb_attached() is True:
            break
        time.sleep(0.5)
    else:
        delete_qmp_usb()
        raise RuntimeError(
            "QEMU created usb-host but could not claim the configured controller"
        )
    device = f"{config['vendor_id']}:{config['product_id']}"
    try:
        result = ssh(
            f"for attempt in $(seq 1 20); do lsusb -d {device} >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1",
            check=False,
            capture=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        delete_qmp_usb()
        raise RuntimeError(
            "guest USB detection timed out after QEMU claimed the controller"
        ) from error
    if result.returncode != 0:
        delete_qmp_usb()
        raise RuntimeError(
            "configured controller was claimed by QEMU but did not appear in the guest"
        )
    ssh("sudo systemctl start bluetooth.service", timeout=60)
    wait_for_guest_controller()
    print("qemu_usb_attached=true")
    doctor()


def usb_detach() -> None:
    if qmp_available():
        fixture_stop(check=False)
        ssh("sudo systemctl stop bluetooth.service", check=False, timeout=30)
        delete_qmp_usb()
    else:
        print("qemu_usb_detached=vm-not-running")


def bluez_restart() -> None:
    if not qmp_available() or qmp_usb_attached() is not True:
        raise RuntimeError(
            "the running VM must own the configured USB controller before restarting BlueZ"
        )
    fixture_stop(check=False)
    ssh("sudo systemctl restart bluetooth.service", timeout=60)
    wait_for_guest_controller()
    fixture_start()
    doctor()
    print("bluez_restart_recovery=pass")


def guest_reboot() -> None:
    if not qmp_available() or qmp_usb_attached() is not True:
        raise RuntimeError(
            "the running VM must own the configured USB controller before guest reboot"
        )
    boot_id_before = ssh(
        "cat /proc/sys/kernel/random/boot_id", capture=True, timeout=10
    ).stdout.strip()
    if not boot_id_before:
        raise RuntimeError("guest boot ID was empty before reboot")
    ssh("sudo systemctl reboot", check=False, capture=True, timeout=30)

    offline_deadline = time.monotonic() + 60
    while time.monotonic() < offline_deadline:
        try:
            result = ssh("true", check=False, capture=True, timeout=5)
        except subprocess.TimeoutExpired:
            break
        if result.returncode != 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("guest SSH never became unavailable during reboot")

    online_deadline = time.monotonic() + 240
    while time.monotonic() < online_deadline:
        if not qmp_available():
            raise RuntimeError("QEMU exited instead of retaining the rebooted guest")
        qmp_status = qmp_execute("query-status")
        if qmp_status.get("status") == "paused":
            raise RuntimeError(
                "QEMU paused during guest reboot; inspect qemu.log for an accelerator failure"
            )
        try:
            result = ssh(
                "cat /proc/sys/kernel/random/boot_id",
                check=False,
                capture=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            boot_id_after = result.stdout.strip()
            if not boot_id_after:
                raise RuntimeError("guest boot ID was empty after reboot")
            if boot_id_after == boot_id_before:
                raise RuntimeError("guest SSH returned without a changed boot ID")
            print("guest_ssh=ready")
            break
        time.sleep(3)
    else:
        raise RuntimeError("guest SSH did not become ready within 240s after reboot")

    wait_for_guest_controller(timeout=60)
    fixture_start()
    doctor()
    print("guest_reboot_recovery=pass")


def require_fixture_environment() -> str:
    expected = fixture_environment_fingerprint()
    actual = guest_fingerprint(
        f"{GUEST_CURRENT_ENVIRONMENT}/blehub-environment.json",
        "fixture_environment_fingerprint",
    )
    if actual != expected:
        raise RuntimeError(
            "the guest fixture environment is missing or stale; run "
            "`pixi run just peripheral vm-fixture-install`"
        )
    return expected


def fixture_install() -> None:
    if not qmp_available():
        raise RuntimeError("the VM is not running")
    wait_for_ssh(timeout=30)
    fingerprint = fixture_environment_fingerprint()
    final_directory = f"{GUEST_ENVIRONMENTS_ROOT}/{fingerprint}"
    marker_path = f"{final_directory}/blehub-environment.json"
    if guest_fingerprint(marker_path, "fixture_environment_fingerprint") == fingerprint:
        activate_guest_link(
            "current-environment", f"environments/{fingerprint}", fingerprint
        )
        print(f"fixture_environment_fingerprint={fingerprint}")
        print("fixture_environment_cached=true")
        return

    staging_directory = (
        f"{GUEST_ENVIRONMENTS_ROOT}/.staging-{fingerprint}-{secrets.token_hex(8)}"
    )
    exists = ssh(
        f"sudo test -e {shlex.quote(final_directory)}",
        check=False,
        capture=True,
        timeout=30,
    )
    if exists.returncode == 0:
        raise RuntimeError(
            f"refusing to replace invalid fixture environment {final_directory}"
        )
    ssh(
        f"sudo rm -rf {shlex.quote(staging_directory)} && "
        f"sudo install -d -o {GUEST_USER} -g {GUEST_USER} -m 0755 "
        f"{shlex.quote(staging_directory)}",
        timeout=30,
    )
    scp_verified(
        (FIXTURE_ROOT / "pixi.toml", FIXTURE_ROOT / "pixi.lock"),
        staging_directory,
    )
    ssh(
        "/opt/blehub/pixi/bin/pixi install --locked --manifest-path "
        f"{shlex.quote(f'{staging_directory}/pixi.toml')}",
        timeout=1800,
    )
    if fixture_environment_fingerprint() != fingerprint:
        raise RuntimeError("fixture environment inputs changed during deployment")
    write_guest_json(
        path=f"{staging_directory}/blehub-environment.json",
        document={
            "schema_version": 1,
            "fixture_environment_fingerprint": fingerprint,
        },
    )
    ssh(
        f"sudo mv {shlex.quote(staging_directory)} {shlex.quote(final_directory)}",
        timeout=60,
    )
    activate_guest_link(
        "current-environment", f"environments/{fingerprint}", fingerprint
    )
    print(f"fixture_environment_fingerprint={fingerprint}")
    print("fixture_environment_cached=false")


def fixture_sync() -> None:
    require_fixture_environment()
    fingerprint = fixture_fingerprint()
    final_directory = f"{GUEST_RELEASES_ROOT}/{fingerprint}"
    marker_path = f"{final_directory}/blehub-fixture.json"
    if guest_fingerprint(marker_path, "fixture_source_fingerprint") == fingerprint:
        activate_guest_link("current", f"releases/{fingerprint}", fingerprint)
        print(f"fixture_source_fingerprint={fingerprint}")
        print("fixture_source_cached=true")
        return

    staging_directory = (
        f"{GUEST_RELEASES_ROOT}/.staging-{fingerprint}-{secrets.token_hex(8)}"
    )
    exists = ssh(
        f"sudo test -e {shlex.quote(final_directory)}",
        check=False,
        capture=True,
        timeout=30,
    )
    if exists.returncode == 0:
        raise RuntimeError(
            f"refusing to replace invalid fixture release {final_directory}"
        )
    ssh(
        f"sudo rm -rf {shlex.quote(staging_directory)} && "
        f"sudo install -d -o {GUEST_USER} -g {GUEST_USER} -m 0755 "
        f"{shlex.quote(staging_directory)}",
        timeout=30,
    )
    scp_verified((FIXTURE_ROOT / "bless_fixture.py", SCENARIO_PATH), staging_directory)
    guest_scenario = f"{staging_directory}/{GUEST_SCENARIO_NAME}"
    ssh(
        f"mv {shlex.quote(f'{staging_directory}/{SCENARIO_PATH.name}')} "
        f"{shlex.quote(guest_scenario)}",
        timeout=30,
    )
    python = f"{GUEST_CURRENT_ENVIRONMENT}/.pixi/envs/default/bin/python"
    ssh(
        f"{shlex.quote(python)} "
        f"{shlex.quote(f'{staging_directory}/bless_fixture.py')} validate "
        f"--scenario {shlex.quote(guest_scenario)}",
        timeout=120,
    )
    if fixture_fingerprint() != fingerprint:
        raise RuntimeError("fixture source inputs changed during deployment")
    write_guest_json(
        path=f"{staging_directory}/blehub-fixture.json",
        document={
            "schema_version": 1,
            "fixture_source_fingerprint": fingerprint,
        },
    )
    ssh(
        f"sudo mv {shlex.quote(staging_directory)} {shlex.quote(final_directory)}",
        timeout=60,
    )
    activate_guest_link("current", f"releases/{fingerprint}", fingerprint)
    print(f"fixture_source_fingerprint={fingerprint}")
    print("fixture_source_cached=false")


def fixture_status_ready(document: dict[str, Any]) -> bool:
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    advertising = scenario.get("advertising")
    if not isinstance(advertising, dict):
        raise TypeError("fixture scenario advertising must be an object")
    return (
        document.get("schema_version") == 1
        and document.get("state") == "ready"
        and document.get("profile_id") == scenario.get("profile_id")
        and document.get("service_uuids") == advertising.get("service_uuids")
    )


def report_fixture_service_log() -> None:
    ssh(
        "sudo journalctl -u blehub-peripheral.service --no-pager -n 100",
        check=False,
        timeout=30,
    )


def restore_fixture_release(previous_target: str | None) -> None:
    ssh(
        "sudo systemctl stop blehub-peripheral.service",
        check=False,
        timeout=60,
    )
    current_target = guest_link_target("current")
    if previous_target is None or previous_target == current_target:
        return
    if not previous_target.startswith("releases/"):
        raise RuntimeError(f"refusing to restore unexpected target {previous_target!r}")
    activate_guest_link("current", previous_target, secrets.token_hex(8))
    result = ssh(
        "sudo systemctl start blehub-peripheral.service",
        check=False,
        capture=True,
        timeout=60,
    )
    print(f"fixture_release_rolled_back={previous_target}")
    print(f"fixture_rollback_started={str(result.returncode == 0).lower()}")


def fixture_start() -> None:
    if not qmp_available():
        raise RuntimeError(
            "the Linux Peripheral VM must be running before starting BLESS"
        )
    require_fixture_environment()
    previous_target = guest_link_target("current")
    fixture_sync()
    ssh(
        "sudo systemctl stop blehub-peripheral.service && "
        "sudo rm -f /run/blehub-peripheral/status.json",
        timeout=60,
    )
    try:
        start_result = ssh(
            "sudo systemctl start blehub-peripheral.service",
            check=False,
            capture=True,
            timeout=60,
        )
        if start_result.returncode != 0:
            report_fixture_service_log()
            raise RuntimeError("the BLESS fixture service could not start")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = ssh(
                "sudo test -f /run/blehub-peripheral/status.json && "
                "sudo cat /run/blehub-peripheral/status.json",
                check=False,
                capture=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    status_document = json.loads(result.stdout)
                except json.JSONDecodeError:
                    status_document = None
                if isinstance(status_document, dict):
                    if status_document.get("state") == "failed":
                        detail = status_document.get("error", "unknown fixture error")
                        report_fixture_service_log()
                        raise RuntimeError(f"the BLESS fixture failed: {detail}")
                    if fixture_status_ready(status_document):
                        service = ssh(
                            "systemctl is-active --quiet blehub-peripheral.service",
                            check=False,
                            capture=True,
                            timeout=10,
                        )
                        if service.returncode == 0:
                            print(
                                "fixture_status="
                                f"{json.dumps(status_document, separators=(',', ':'))}"
                            )
                            print("fixture_running=true")
                            return
            service = ssh(
                "systemctl is-failed --quiet blehub-peripheral.service",
                check=False,
                capture=True,
                timeout=10,
            )
            if service.returncode == 0:
                report_fixture_service_log()
                raise RuntimeError("the BLESS fixture service failed")
            time.sleep(1)
        raise RuntimeError("the BLESS fixture did not report readiness within 30s")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        restore_fixture_release(previous_target)
        raise


def normalize_control_value_hex(value_hex: str) -> str:
    try:
        value = bytes.fromhex(value_hex)
    except ValueError as error:
        raise ValueError("fixture update value must be valid hex") from error
    if len(value_hex) != len(value) * 2 or value.hex() != value_hex.lower():
        raise ValueError("fixture update value must contain only pairs of hex digits")
    return value.hex()


def normalize_burst_id(burst_id: str) -> str:
    try:
        value = bytes.fromhex(burst_id)
    except ValueError as error:
        raise ValueError("fixture burst ID must be valid hex") from error
    if len(burst_id) != 16 or len(value) != 8 or value.hex() != burst_id:
        raise ValueError("fixture burst ID must be 16 lowercase hex digits")
    return burst_id


def normalize_burst_count(count_text: str) -> int:
    if not count_text.isascii() or not count_text.isdecimal():
        raise ValueError("fixture burst count must be a decimal integer")
    count = int(count_text)
    if not 1 <= count <= 10_000:
        raise ValueError("fixture burst count must be from 1 through 10000")
    return count


def normalize_payload_bytes(payload_bytes_text: str) -> int:
    if not payload_bytes_text.isascii() or not payload_bytes_text.isdecimal():
        raise ValueError("fixture payload_bytes must be a decimal integer")
    payload_bytes = int(payload_bytes_text)
    if not 20 <= payload_bytes <= 512:
        raise ValueError("fixture payload_bytes must be from 20 through 512")
    return payload_bytes


def normalize_burst_interval_us(interval_us_text: str) -> int:
    if not interval_us_text.isascii() or not interval_us_text.isdecimal():
        raise ValueError("fixture burst interval_us must be a decimal integer")
    interval_us = int(interval_us_text)
    if not 0 <= interval_us <= 1_000_000:
        raise ValueError("fixture burst interval_us must be from 0 through 1000000")
    return interval_us


def require_fixture_ready() -> dict[str, Any]:
    if not qmp_available():
        raise RuntimeError("the VM is not running")
    service = ssh(
        "systemctl is-active --quiet blehub-peripheral.service",
        check=False,
        capture=True,
        timeout=10,
    )
    status_document = guest_json(f"{GUEST_RUNTIME_ROOT}/status.json", timeout=10)
    if service.returncode != 0 or status_document is None:
        raise RuntimeError("the BLESS fixture is not running and ready")
    if not fixture_status_ready(status_document):
        raise RuntimeError("the BLESS fixture readiness document is stale or invalid")
    run_id = status_document.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("the BLESS fixture readiness document has no run_id")
    return status_document


def fixture_update(value_hex: str, *, timeout: float = 15.0) -> None:
    normalized_value = normalize_control_value_hex(value_hex)
    status_document = require_fixture_ready()
    run_id = status_document["run_id"]
    control_id = secrets.token_hex(16)
    write_guest_runtime_json(
        GUEST_CONTROL_REQUEST,
        {
            "schema_version": 1,
            "control_id": control_id,
            "run_id": run_id,
            "value_hex": normalized_value,
        },
    )
    ssh(
        "sudo systemctl kill --kill-who=main --signal=SIGUSR1 "
        "blehub-peripheral.service",
        timeout=10,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = guest_json(GUEST_CONTROL_RESPONSE, timeout=5)
        if response is not None and response.get("control_id") == control_id:
            if response.get("schema_version") != 1:
                raise RuntimeError("the fixture update response has an invalid schema")
            if response.get("state") == "failed":
                detail = response.get("error", "unknown fixture control error")
                raise RuntimeError(f"the BLESS fixture rejected the update: {detail}")
            if response.get("run_id") != run_id:
                raise RuntimeError(
                    "the active fixture run changed while processing the update"
                )
            if (
                response.get("state") != "updated"
                or response.get("value_hex") != normalized_value
            ):
                raise RuntimeError("the fixture update response is invalid")
            print(
                "fixture_update_response="
                f"{json.dumps(response, separators=(',', ':'), sort_keys=True)}"
            )
            print("fixture_updated=true")
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"the BLESS fixture did not respond to control {control_id} within {timeout:g}s"
    )


def fixture_burst(
    burst_id: str,
    count_text: str,
    interval_us_text: str = "0",
    payload_bytes_text: str = "20",
    *,
    timeout: float = 120.0,
) -> None:
    normalized_burst_id = normalize_burst_id(burst_id)
    count = normalize_burst_count(count_text)
    interval_us = normalize_burst_interval_us(interval_us_text)
    payload_bytes = normalize_payload_bytes(payload_bytes_text)
    status_document = require_fixture_ready()
    run_id = status_document["run_id"]
    control_id = secrets.token_hex(16)
    write_guest_runtime_json(
        GUEST_CONTROL_REQUEST,
        {
            "schema_version": 1,
            "operation": "notification_burst",
            "control_id": control_id,
            "run_id": run_id,
            "burst_id": normalized_burst_id,
            "count": count,
            "interval_us": interval_us,
            "payload_bytes": payload_bytes,
        },
    )
    ssh(
        "sudo systemctl kill --kill-who=main --signal=SIGUSR1 "
        "blehub-peripheral.service",
        timeout=10,
    )

    completed_fields = {
        "schema_version",
        "state",
        "control_id",
        "run_id",
        "burst_id",
        "count",
        "interval_us",
        "payload_bytes",
        "accepted_count",
        "local_elapsed_ns",
    }
    failed_fields = completed_fields | {"sequence", "error"}
    pacing_seconds = (count - 1) * interval_us / 1_000_000
    deadline = time.monotonic() + max(timeout, pacing_seconds + 15.0)
    while time.monotonic() < deadline:
        response = guest_json(GUEST_CONTROL_RESPONSE, timeout=5)
        if response is not None and response.get("control_id") == control_id:
            state = response.get("state")
            expected_fields = failed_fields if state == "failed" else completed_fields
            if set(response) != expected_fields:
                raise RuntimeError("the fixture burst response has invalid fields")
            if (
                type(response.get("schema_version")) is not int
                or response.get("schema_version") != 1
            ):
                raise RuntimeError("the fixture burst response has an invalid schema")
            if response.get("run_id") != run_id:
                raise RuntimeError(
                    "the active fixture run changed while processing the burst"
                )
            if response.get("burst_id") != normalized_burst_id:
                raise RuntimeError("the fixture burst response has an invalid burst ID")
            if type(response.get("count")) is not int or response.get("count") != count:
                raise RuntimeError("the fixture burst response has an invalid count")
            if (
                type(response.get("interval_us")) is not int
                or response.get("interval_us") != interval_us
            ):
                raise RuntimeError(
                    "the fixture burst response has an invalid interval_us"
                )
            if response.get("payload_bytes") != payload_bytes:
                raise RuntimeError(
                    "the fixture burst response has an invalid payload_bytes"
                )
            accepted_count = response.get("accepted_count")
            if type(accepted_count) is not int or not 0 <= accepted_count <= count:
                raise RuntimeError(
                    "the fixture burst response has an invalid accepted count"
                )
            local_elapsed_ns = response.get("local_elapsed_ns")
            if type(local_elapsed_ns) is not int or local_elapsed_ns < 0:
                raise RuntimeError(
                    "the fixture burst response has invalid local elapsed time"
                )
            if state == "failed":
                sequence = response.get("sequence")
                error = response.get("error")
                if type(sequence) is not int or sequence != accepted_count:
                    raise RuntimeError(
                        "the failed fixture burst response has an invalid sequence"
                    )
                if not isinstance(error, str) or not error:
                    raise RuntimeError(
                        "the failed fixture burst response has no error detail"
                    )
                raise RuntimeError(
                    "the BLESS fixture rejected the burst at sequence "
                    f"{sequence}: {error}"
                )
            if state != "completed" or accepted_count != count:
                raise RuntimeError("the fixture burst response is invalid")
            print(
                "fixture_burst_response="
                f"{json.dumps(response, separators=(',', ':'), sort_keys=True)}"
            )
            print(f"fixture_local_elapsed_ns={local_elapsed_ns}")
            print("fixture_burst_completed=true")
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"the BLESS fixture did not respond to control {control_id} within {timeout:g}s"
    )


def fixture_write_batch(
    operation: str,
    batch_id: str,
    count_text: str | None = None,
    *,
    timeout: float = 30.0,
) -> None:
    normalized_batch_id = normalize_burst_id(batch_id)
    count = normalize_burst_count(count_text) if count_text is not None else None
    status_document = require_fixture_ready()
    run_id = status_document["run_id"]
    control_id = secrets.token_hex(16)
    request: dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        "control_id": control_id,
        "run_id": run_id,
        "batch_id": normalized_batch_id,
    }
    if count is not None:
        request["count"] = count
    write_guest_runtime_json(GUEST_CONTROL_REQUEST, request)
    ssh(
        "sudo systemctl kill --kill-who=main --signal=SIGUSR1 "
        "blehub-peripheral.service",
        timeout=10,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = guest_json(GUEST_CONTROL_RESPONSE, timeout=5)
        if response is not None and response.get("control_id") == control_id:
            if (
                response.get("schema_version") != 1
                or response.get("run_id") != run_id
                or response.get("operation") != operation
                or response.get("batch_id") != normalized_batch_id
            ):
                raise RuntimeError("the fixture write batch response is invalid")
            if operation == "write_batch_start":
                if (
                    response.get("state") != "started"
                    or response.get("expected_count") != count
                ):
                    raise RuntimeError(
                        "the fixture write batch start response is invalid"
                    )
            else:
                counter_fields = (
                    "expected_count",
                    "received_count",
                    "unique_count",
                    "missing_count",
                    "duplicate_count",
                    "out_of_order_count",
                    "out_of_range_count",
                    "foreign_batch_count",
                    "malformed_count",
                    "callback_span_ns",
                )
                if response.get("state") != "reported" or any(
                    type(response.get(field)) is not int or response[field] < 0
                    for field in counter_fields
                ):
                    raise RuntimeError(
                        "the fixture write batch status response is invalid"
                    )
            print(
                "fixture_write_batch_response="
                f"{json.dumps(response, separators=(',', ':'), sort_keys=True)}"
            )
            if operation == "write_batch_status":
                fields = (
                    "batch_id",
                    "expected_count",
                    "received_count",
                    "unique_count",
                    "missing_count",
                    "duplicate_count",
                    "out_of_order_count",
                    "out_of_range_count",
                    "foreign_batch_count",
                    "malformed_count",
                    "callback_span_ns",
                )
                print(
                    "fixture_write_batch_status "
                    + " ".join(f"{field}={response[field]}" for field in fields)
                )
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"the BLESS fixture did not respond to write batch control {control_id} within {timeout:g}s"
    )


def fixture_stop(*, check: bool = True) -> None:
    if not qmp_available():
        if check:
            raise RuntimeError("the VM is not running")
        return
    ssh(
        "sudo systemctl stop blehub-peripheral.service",
        check=check,
        timeout=60,
    )
    print("fixture_running=false")


def fixture_status() -> None:
    if not qmp_available():
        print("fixture_running=false")
        print("fixture_detail=vm-not-running")
        return
    result = ssh(
        "systemctl is-active blehub-peripheral.service; sudo cat /run/blehub-peripheral/status.json 2>/dev/null || true",
        check=False,
        capture=True,
        timeout=30,
    )
    print(result.stdout.strip())


def _connected_le_handle() -> int:
    deadline = time.monotonic() + 30.0
    while True:
        result = ssh("hcitool con", capture=True, timeout=15)
        handles = [
            int(value) for value in re.findall(r"\bhandle (\d+)\b", result.stdout)
        ]
        if len(handles) == 1:
            return handles[0]
        if len(handles) > 1:
            raise RuntimeError(
                f"expected one active Bluetooth connection, found {len(handles)}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for an active Bluetooth connection")
        time.sleep(0.1)


def _read_active_phy(handle: int) -> tuple[int, int]:
    handle_low = handle & 0xFF
    handle_high = handle >> 8
    deadline = time.monotonic() + 5.0
    last_output = ""
    while True:
        result = ssh(
            f"sudo -n hcitool -i hci0 cmd 08 0030 {handle_low:02x} {handle_high:02x}",
            capture=True,
            timeout=15,
        )
        last_output = f"{result.stdout}\n{result.stderr}"
        for line in reversed(last_output.splitlines()):
            fields = line.split()
            if len(fields) != 8 or any(
                len(field) != 2
                or any(character not in "0123456789abcdefABCDEF" for character in field)
                for field in fields
            ):
                continue
            response = bytes.fromhex("".join(fields))
            response_handle = int.from_bytes(response[4:6], "little")
            if response[1:3] != b"\x30\x20" or response_handle != handle:
                continue
            if response[3] != 0:
                raise RuntimeError(
                    f"LE Read PHY failed with HCI status 0x{response[3]:02x}"
                )
            return response[6], response[7]
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "LE Read PHY returned no parseable Command Complete event after retries: "
                f"{last_output.strip()!r}"
            )
        time.sleep(0.05)


def fixture_request_throughput_phy() -> None:
    handle = _connected_le_handle()
    before_tx, before_rx = _read_active_phy(handle)
    handle_low = handle & 0xFF
    handle_high = handle >> 8
    ssh(
        f"sudo -n hcitool -i hci0 cmd 08 0032 {handle_low:02x} {handle_high:02x} 00 02 02 00 00",
        capture=True,
        timeout=15,
    )
    deadline = time.monotonic() + 5.0
    while True:
        tx_phy, rx_phy = _read_active_phy(handle)
        if tx_phy == 2 and rx_phy == 2:
            print(
                f"fixture_link_handle={handle} phy_before={before_tx}/{before_rx} phy_after=2/2"
            )
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"active link PHY remained {tx_phy}/{rx_phy} after requesting LE 2M"
            )
        time.sleep(0.1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the project Linux Peripheral VM and BLESS fixture"
    )
    parser.add_argument(
        "command",
        choices=(
            "bootstrap",
            "build",
            "doctor",
            "start",
            "stop",
            "reset",
            "status",
            "logs",
            "clean",
            "usb-status",
            "usb-attach",
            "usb-detach",
            "bluez-restart",
            "guest-reboot",
            "fixture-install",
            "fixture-sync",
            "fixture-start",
            "fixture-update",
            "fixture-burst",
            "fixture-write-batch-start",
            "fixture-write-batch-status",
            "fixture-request-throughput-phy",
            "fixture-stop",
            "fixture-status",
            "whpx-status",
            "whpx-enable",
        ),
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("count", nargs="?")
    parser.add_argument("interval_us", nargs="?")
    parser.add_argument("payload_bytes", nargs="?")
    args = parser.parse_args()
    if args.command == "fixture-update":
        if args.value is None:
            parser.error("fixture-update requires value_hex")
        if (
            args.count is not None
            or args.interval_us is not None
            or args.payload_bytes is not None
        ):
            parser.error("fixture-update accepts only value_hex")
    elif args.command in {"fixture-burst", "fixture-write-batch-start"}:
        if args.value is None or args.count is None:
            parser.error(f"{args.command} requires batch_id and count")
        if args.command == "fixture-write-batch-start" and (
            args.interval_us is not None or args.payload_bytes is not None
        ):
            parser.error("fixture-write-batch-start accepts only batch_id and count")
    elif args.command == "fixture-write-batch-status":
        if args.value is None:
            parser.error("fixture-write-batch-status requires batch_id")
        if (
            args.count is not None
            or args.interval_us is not None
            or args.payload_bytes is not None
        ):
            parser.error("fixture-write-batch-status accepts only batch_id")
    elif (
        args.value is not None
        or args.count is not None
        or args.interval_us is not None
        or args.payload_bytes is not None
    ):
        parser.error(f"{args.command} does not accept arguments")
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.command == "bootstrap":
            bootstrap()
        elif args.command == "build":
            build()
        elif args.command == "doctor":
            doctor()
        elif args.command == "start":
            start()
        elif args.command == "stop":
            stop_running_vm()
        elif args.command == "reset":
            reset()
        elif args.command == "status":
            status()
        elif args.command == "logs":
            logs()
        elif args.command == "clean":
            clean()
        elif args.command == "usb-status":
            usb_status()
        elif args.command == "usb-attach":
            usb_attach()
        elif args.command == "usb-detach":
            usb_detach()
        elif args.command == "bluez-restart":
            bluez_restart()
        elif args.command == "guest-reboot":
            guest_reboot()
        elif args.command == "fixture-install":
            fixture_install()
        elif args.command == "fixture-sync":
            fixture_sync()
        elif args.command == "fixture-start":
            fixture_start()
        elif args.command == "fixture-update":
            fixture_update(args.value)
        elif args.command == "fixture-burst":
            fixture_burst(
                args.value,
                args.count,
                args.interval_us or "0",
                args.payload_bytes or "20",
            )
        elif args.command == "fixture-write-batch-start":
            fixture_write_batch("write_batch_start", args.value, args.count)
        elif args.command == "fixture-write-batch-status":
            fixture_write_batch("write_batch_status", args.value)
        elif args.command == "fixture-request-throughput-phy":
            fixture_request_throughput_phy()
        elif args.command == "fixture-stop":
            fixture_stop()
        elif args.command == "fixture-status":
            fixture_status()
        elif args.command == "whpx-status":
            whpx_status()
        else:
            whpx_enable()
    except (
        EOFError,
        OSError,
        QMPError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
