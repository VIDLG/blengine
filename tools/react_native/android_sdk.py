"""Owned Android SDK/ADB command boundary for maintained RN tooling.

This module owns device command construction, bounded subprocess execution and
permission/logcat readback. It does not own RF scenarios, runtime marker ledgers,
fixture policy or final verdicts.
"""

from __future__ import annotations

import math
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from tools.android.run_instrumentation import has_root
from tools.android.run_instrumentation import install_apk as install_apk_via_adb
from tools.android_toolchain import adb_executable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANDROID_PACKAGE = "com.blehubnitrohost"


class AndroidSdkError(RuntimeError):
    """Raised when an Android SDK command cannot complete safely."""


def adb_command(serial: str, *arguments: str) -> list[str]:
    return [str(adb_executable()), "-s", serial, *arguments]


def run_command(
    command: list[str], *, cwd: Path = PROJECT_ROOT, timeout: float, check: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AndroidSdkError(
            f"command failed to start or finish: {' '.join(command)}"
        ) from error
    if check and result.returncode:
        raise AndroidSdkError(
            f"command failed (exit={result.returncode}): {' '.join(command)}; "
            f"{result.stdout[-1500:]}"
        )
    return result


def require_device(serial: str, *, timeout: float = 15) -> None:
    """Require an explicitly selected physical Android device to be ready."""
    result = run_command(
        adb_command(serial, "get-state"),
        timeout=timeout,
    )
    if result.returncode or result.stdout.strip() != "device":
        raise AndroidSdkError(
            f"Android device {serial!r} is not ready: {result.stdout.strip()!r}"
        )


def install_apk(
    serial: str,
    apk: Path,
    *,
    log: TextIO,
    test_only: bool = False,
    use_root: bool | None = None,
    grant: bool = False,
    deadline: float | None = None,
    require_owner: Callable[[], None] | None = None,
    cwd: Path | None = None,
) -> bool:
    """Install once through the shared push/pm install/cleanup boundary.

    Legacy callers may retain automatic root detection (``use_root=None``).
    Guarded callers must select a mode explicitly, so detection cannot bypass
    their per-command owner checks and shared absolute monotonic deadline.
    """
    if use_root is None and (deadline is not None or require_owner is not None):
        raise ValueError("guarded APK installation requires an explicit use_root")
    if not apk.is_file():
        raise AndroidSdkError(f"missing APK: {apk}")
    adb = adb_executable()
    if use_root is None:
        try:
            use_root = has_root(adb, serial)
        except (OSError, subprocess.SubprocessError) as error:
            raise AndroidSdkError("unable to determine Android install mode") from error
    try:
        install_apk_via_adb(
            adb,
            serial,
            apk,
            test_only=test_only,
            use_root=use_root,
            grant=grant,
            log=log,
            **({"deadline": deadline} if deadline is not None else {}),
            **({"require_owner": require_owner} if require_owner is not None else {}),
            **({"cwd": cwd} if cwd is not None else {}),
        )
    except Exception as error:  # preserve the SDK boundary for all install failures
        failure = AndroidSdkError(f"APK installation failed: {error}")
        for note in getattr(error, "__notes__", ()):
            failure.add_note(note)
        raise failure from error
    return bool(use_root)


def logcat(serial: str, *, cwd: Path = PROJECT_ROOT, timeout: float = 15) -> str:
    return run_command(
        adb_command(serial, "logcat", "-d", "-v", "brief"),
        cwd=cwd,
        timeout=timeout,
        check=True,
    ).stdout


def runtime_alive(serial: str, *, cwd: Path = PROJECT_ROOT, timeout: float = 5) -> bool:
    result = run_command(
        adb_command(serial, "shell", "pidof", ANDROID_PACKAGE),
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


@dataclass(frozen=True)
class AndroidAppIdentity:
    """Read-only OS/install snapshot, not native startup or BLE evidence."""

    serial: str
    package: str
    pid: int
    uid: int
    cmdline: str
    start_ticks: int
    boot_id: str
    base_apk_path: str
    apk_sha256: str


def read_app_identity(
    *,
    serial: str,
    package: str,
    expected_apk_sha256: str,
    expected_identity: AndroidAppIdentity | None,
    cwd: Path,
    deadline: float,
    require_owner: Callable[[], None],
) -> AndroidAppIdentity:
    """Pin one main process and one base.apk using only shell read commands.

    ``deadline`` is an absolute monotonic deadline shared by every command and
    owner check, including the caller's wider operation. Procfs denial, split APKs,
    multiple PIDs and unsupported sha256sum fail closed; no root/pull fallback.
    Owner checks run before and after each attempted command, including failures.
    This is a bracketed observation, not an atomic OS snapshot or device attestation.
    """
    # permission_control already depends on this SDK boundary. Delay this import
    # to reuse its proc-stat parser without a module-initialization cycle.
    from tools.android.permission_control import _start_ticks

    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
        raise ValueError("an explicit canonical Android serial is required")
    if not isinstance(package, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package
    ):
        raise ValueError("an explicit canonical Android package is required")
    if not isinstance(expected_apk_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_apk_sha256
    ):
        raise ValueError("expected_apk_sha256 must be an explicit lowercase SHA256")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("deadline must be a finite absolute monotonic time")
    if expected_identity is not None and (
        type(expected_identity) is not AndroidAppIdentity
        or expected_identity.serial != serial
        or expected_identity.package != package
        or expected_identity.apk_sha256 != expected_apk_sha256
    ):
        raise ValueError(
            "expected_identity differs from explicit serial/package/candidate"
        )
    if not isinstance(cwd, Path) or not callable(require_owner):
        raise TypeError("cwd and require_owner must be explicit Path/callback values")

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Android App identity total deadline exceeded")
        return budget

    def command(*args: str) -> str:
        remaining()
        require_owner()
        remaining()
        try:
            arguments = adb_command(serial, "shell", *args)
            result = run_command(arguments, cwd=cwd, timeout=remaining(), check=True)
        except Exception as error:
            try:
                require_owner()
                remaining()
            except Exception as boundary:  # noqa: BLE001 -- retain the primary SDK failure.
                error.add_note(f"Android identity post-command boundary: {boundary}")
            raise
        require_owner()
        remaining()
        if result.returncode or not isinstance(result.stdout, str):
            raise AndroidSdkError("Android App identity read command failed")
        if len(result.stdout.encode("utf-8")) > 64 * 1024:
            raise AndroidSdkError("oversized Android App identity readback")
        return result.stdout

    def boot_id() -> str:
        value = command("cat", "/proc/sys/kernel/random/boot_id").removesuffix("\n")
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
            raise AndroidSdkError("invalid or ambiguous OS boot_id")
        return value

    def pid() -> int:
        value = command("pidof", package).removesuffix("\n")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2**31 - 1:
            raise AndroidSdkError("missing, ambiguous or invalid App PID")
        return int(value)

    def apk_path() -> str:
        value = command("pm", "path", package).removesuffix("\n")
        # Restrict shell metacharacters as adb shell joins argv on the device.
        # Refuse split packages rather than silently hashing only their base.
        if not re.fullmatch(r"package:/data/app/[A-Za-z0-9_./=+~-]+/base\.apk", value):
            raise AndroidSdkError("missing, split or ambiguous installed base.apk path")
        path = value[len("package:") :]
        if any(part in ("", ".", "..") for part in path.split("/")[1:]):
            raise AndroidSdkError("noncanonical installed base.apk path")
        return path

    def ticks(process: int) -> int:
        output = command("cat", f"/proc/{process}/stat")
        try:
            return _start_ticks(output, process)
        except RuntimeError as error:
            raise AndroidSdkError(str(error)) from error

    def uid(process: int) -> int:
        status = command("cat", f"/proc/{process}/status")
        pids = re.findall(r"(?m)^Pid:[ \t]+([0-9]+)[ \t]*$", status)
        values = re.findall(
            r"(?m)^Uid:[ \t]+([0-9]{1,10})[ \t]+([0-9]{1,10})[ \t]+([0-9]{1,10})[ \t]+([0-9]{1,10})[ \t]*$",
            status,
        )
        if (
            pids != [str(process)]
            or len(re.findall(r"(?m)^Pid:", status)) != 1
            or len(re.findall(r"(?m)^Uid:", status)) != 1
            or len(values) != 1
            or len(set(values[0])) != 1
            or int(values[0][0]) < 10000
        ):
            raise AndroidSdkError("invalid or ambiguous process PID/UID status")
        return int(values[0][0])

    def cmdline(process: int) -> str:
        raw = command("cat", f"/proc/{process}/cmdline")
        # Android may pad argv storage with NULs after the exact main package.
        if not raw.endswith("\0") or raw.rstrip("\0") != package:
            raise AndroidSdkError("process cmdline differs from exact main package")
        return package

    boot = boot_id()
    if expected_identity is not None and boot != expected_identity.boot_id:
        raise AndroidSdkError("pinned OS boot_id changed")
    process = pid()
    if expected_identity is not None and process != expected_identity.pid:
        raise AndroidSdkError("pinned App PID changed; refusing to reselect")
    path = apk_path()
    if expected_identity is not None and path != expected_identity.base_apk_path:
        raise AndroidSdkError("pinned installed APK path changed")
    start = ticks(process)
    if expected_identity is not None and start != expected_identity.start_ticks:
        raise AndroidSdkError("pinned App start_ticks changed (restart/PID reuse)")
    user = uid(process)
    name = cmdline(process)
    if expected_identity is not None and (
        user != expected_identity.uid or name != expected_identity.cmdline
    ):
        raise AndroidSdkError("pinned App UID/cmdline changed")
    digest_output = command("sha256sum", path)
    if digest_output != f"{expected_apk_sha256}  {path}\n":
        raise AndroidSdkError(
            "installed APK SHA256/candidate mismatch or invalid hash output"
        )
    # Reverse sweep brackets the hash and checks the original PID, never reselects.
    if (
        cmdline(process) != name
        or uid(process) != user
        or ticks(process) != start
        or apk_path() != path
        or pid() != process
        or boot_id() != boot
    ):
        raise AndroidSdkError("App process/boot/installed path changed during snapshot")
    return AndroidAppIdentity(
        serial=serial,
        package=package,
        pid=process,
        uid=user,
        cmdline=name,
        start_ticks=start,
        boot_id=boot,
        base_apk_path=path,
        apk_sha256=expected_apk_sha256,
    )


def _permission_is_granted(serial: str, permission: str, *, cwd: Path) -> bool:
    output = run_command(
        adb_command(serial, "shell", "dumpsys", "package", ANDROID_PACKAGE),
        cwd=cwd,
        timeout=15,
        check=True,
    ).stdout
    return (
        re.search(
            rf"(?m)^\s*{re.escape(permission)}:\s*granted=true(?:[,\s]|$)", output
        )
        is not None
    )


def grant_bluetooth_permissions(serial: str, *, cwd: Path = PROJECT_ROOT) -> None:
    sdk = run_command(
        adb_command(serial, "shell", "getprop", "ro.build.version.sdk"),
        cwd=cwd,
        timeout=15,
        check=True,
    ).stdout.strip()
    if not sdk.isdecimal() or int(sdk) <= 0:
        raise AndroidSdkError(f"invalid Android device API level: {sdk!r}")
    api = int(sdk)
    permissions = (
        ("android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT")
        if api >= 31
        else ("android.permission.ACCESS_FINE_LOCATION",)
        if api >= 23
        else ()
    )
    if not permissions:
        return
    try:
        use_root = has_root(adb_executable(), serial)
    except (OSError, subprocess.SubprocessError) as error:
        raise AndroidSdkError(
            "unable to determine Android permission grant root access"
        ) from error
    for permission in permissions:
        if _permission_is_granted(serial, permission, cwd=cwd):
            continue
        arguments = (
            ("shell", "su", "-c", f"pm grant {ANDROID_PACKAGE} {permission}")
            if use_root
            else ("shell", "pm", "grant", ANDROID_PACKAGE, permission)
        )
        run_command(adb_command(serial, *arguments), cwd=cwd, timeout=15, check=True)
