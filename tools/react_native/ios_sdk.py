"""Small, owned command API for maintained iOS tooling.

This module builds and bounds Apple SDK commands; it does not own scenario facts,
radio policy, leases, XCTest assertions, or RF verdicts. Callers choose their
logs and decide whether a command is streaming or a one-shot receipt operation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from tests.peripherals.nrf52840.cancellation_control import read_document

PROJECT_ROOT = Path(__file__).resolve().parents[2]
XCRUN = "xcrun"


class IosSdkError(RuntimeError):
    """Raised when a bounded iOS SDK command cannot complete safely."""


_IOS_CONTAINER_EXECUTABLE = re.compile(
    r"^file:///private/var/containers/Bundle/Application/"
    r"[0-9A-Fa-f-]{36}(/[^/]+\.app/[^/]+)$"
)


def ios_executables_match(expected: str, observed: str) -> bool:
    """Match an installed App while allowing XCTest container UUID replacement."""
    if expected == observed:
        return True
    expected_match = _IOS_CONTAINER_EXECUTABLE.fullmatch(expected)
    observed_match = _IOS_CONTAINER_EXECUTABLE.fullmatch(observed)
    return (
        expected_match is not None
        and observed_match is not None
        and expected_match.group(1) == observed_match.group(1)
    )


def official_result(path: Path, command_type: str, device: str) -> dict[str, Any]:
    """Validate and return the result of one recorded CoreDevice JSON receipt."""
    document = read_document(path)
    info, result = document.get("info"), document.get("result")
    if not isinstance(info, dict) or not isinstance(result, dict):
        raise TypeError("incomplete official iOS identity")
    arguments = info.get("arguments")
    if (
        info.get("outcome") != "success"
        or info.get("commandType") != command_type
        or not isinstance(arguments, list)
        or arguments[:4] != command_type.split(".")
        or arguments.count("--device") != 1
        or arguments[arguments.index("--device") + 1 :][:1] != [device]
        or not isinstance(result.get("deviceIdentifier"), str)
        or not result["deviceIdentifier"]
    ):
        raise RuntimeError("foreign official iOS command/device identity")
    return result


def devicectl_list_devices() -> list[str]:
    return [XCRUN, "devicectl", "list", "devices"]


def devicectl_lock_state(device: str) -> list[str]:
    return [
        XCRUN,
        "devicectl",
        "device",
        "info",
        "lockState",
        "--quiet",
        "--device",
        device,
        "--json-output",
        "-",
    ]


def devicectl_pair(device: str, receipt: Path, *, timeout: int = 60) -> list[str]:
    """Build a bounded CoreDevice pairing command without unpairing first."""
    if not device.strip():
        raise ValueError("a physical device identifier is required")
    if timeout <= 0:
        raise ValueError("pairing timeout must be positive")
    return [
        XCRUN,
        "devicectl",
        "manage",
        "pair",
        "--device",
        device,
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt),
    ]


def devicectl_install_app(
    device: str, app: Path, receipt: Path, *, timeout: int | None = None
) -> list[str]:
    return [
        XCRUN,
        "devicectl",
        "device",
        "install",
        "app",
        "--device",
        device,
        "--json-output",
        str(receipt),
        *([] if timeout is None else ["--timeout", str(timeout)]),
        str(app),
    ]


def devicectl_reboot(
    device: str,
    receipt: Path,
    *,
    timeout: int = 180,
    style: str = "full",
) -> list[str]:
    """Build an explicit CoreDevice reboot command with a bounded receipt."""
    if not device.strip():
        raise ValueError("a physical device identifier is required")
    if style not in {"full", "userspace"}:
        raise ValueError("reboot style must be full or userspace")
    if timeout <= 0:
        raise ValueError("reboot timeout must be positive")
    return [
        XCRUN,
        "devicectl",
        "device",
        "reboot",
        "--device",
        device,
        "--style",
        style,
        "--wait-for-device",
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt),
    ]


def devicectl_screenshot(
    device: str, destination: Path, *, timeout: int = 30
) -> list[str]:
    """Build a bounded, read-only CoreDevice screenshot command."""
    if not device.strip():
        raise ValueError("a physical device identifier is required")
    if destination.suffix.lower() != ".png":
        raise ValueError("screenshot destination must be a PNG file")
    if timeout <= 0:
        raise ValueError("screenshot timeout must be positive")
    return [
        XCRUN,
        "devicectl",
        "device",
        "capture",
        "screenshot",
        "--device",
        device,
        "--destination",
        str(destination),
        "--timeout",
        str(timeout),
        "--quiet",
    ]


def devicectl_device_details(device: str) -> list[str]:
    """Build a read-only CoreDevice details query for preflight checks."""
    return [
        XCRUN,
        "devicectl",
        "device",
        "info",
        "details",
        "--device",
        device,
        "--quiet",
        "--json-output",
        "-",
    ]


def devicectl_device_apps(
    device: str, receipt: Path | None = None, *, timeout: int | None = None
) -> list[str]:
    """Build a read-only installed-app inventory query for preflight checks."""
    return [
        XCRUN,
        "devicectl",
        "device",
        "info",
        "apps",
        "--device",
        device,
        "--quiet",
        *([] if timeout is None else ["--timeout", str(timeout)]),
        "--json-output",
        str(receipt) if receipt is not None else "-",
    ]


def devicectl_processes(device: str, receipt: Path, *, timeout: int = 5) -> list[str]:
    return [
        XCRUN,
        "devicectl",
        "device",
        "info",
        "processes",
        "--device",
        device,
        "--quiet",
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt),
    ]


def devicectl_launch_once(
    device: str,
    bundle_id: str,
    receipt: Path,
    arguments: list[str],
    *,
    timeout: int,
) -> list[str]:
    """Return after launch acknowledgement; never terminate or attach a console.

    The host must reject existing processes and validate the launch receipt plus
    fresh process identity. Omitting terminate-existing is not an atomic exclusion.
    """
    return [
        XCRUN,
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        device,
        "--activate",
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt),
        bundle_id,
        "--",
        *arguments,
    ]


def devicectl_launch_app(
    device: str, bundle_id: str, receipt: Path, arguments: list[str]
) -> list[str]:
    return [
        XCRUN,
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        device,
        "--terminate-existing",
        "--activate",
        "--console",
        "--json-output",
        str(receipt),
        bundle_id,
        *arguments,
    ]


def devicectl_launch_payload(
    device: str, bundle_id: str, url: str, receipt: Path
) -> list[str]:
    return [
        XCRUN,
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        device,
        "--quiet",
        "--timeout",
        "5",
        "--payload-url",
        url,
        "--json-output",
        str(receipt),
        bundle_id,
    ]


def devicectl_post_notification(
    device: str,
    name: str,
    receipt: Path | None = None,
    *,
    timeout: int = 5,
) -> list[str]:
    """Build one bounded CoreDevice notification-post command."""
    return [
        XCRUN,
        "devicectl",
        "device",
        "notification",
        "post",
        "--device",
        device,
        "--name",
        name,
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt) if receipt is not None else "-",
    ]


def devicectl_terminate_process(
    device: str, pid: int, receipt: Path, *, quiet: bool = True
) -> list[str]:
    """Build a PID-scoped CoreDevice process termination command."""
    command = [
        XCRUN,
        "devicectl",
        "device",
        "process",
        "terminate",
        "--device",
        device,
        "--pid",
        str(pid),
        "--json-output",
        str(receipt),
    ]
    if quiet:
        command.append("--quiet")
    return command


def devicectl_copy_to(
    device: str,
    source: Path,
    destination: str,
    bundle_id: str,
    receipt: Path,
    *,
    timeout: int,
) -> list[str]:
    """Build a bounded copy-to-App-container command."""
    return [
        XCRUN,
        "devicectl",
        "device",
        "copy",
        "to",
        "--device",
        device,
        "--source",
        str(source),
        "--destination",
        destination,
        "--domain-type",
        "appDataContainer",
        "--domain-identifier",
        bundle_id,
        "--quiet",
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt),
    ]


def devicectl_copy_from(
    device: str,
    source: str,
    bundle_id: str,
    destination: Path,
    receipt: Path,
    *,
    timeout: int,
) -> list[str]:
    return [
        XCRUN,
        "devicectl",
        "device",
        "copy",
        "from",
        "--device",
        device,
        "--source",
        source,
        "--domain-type",
        "appDataContainer",
        "--domain-identifier",
        bundle_id,
        "--destination",
        str(destination),
        "--quiet",
        "--timeout",
        str(timeout),
        "--json-output",
        str(receipt),
    ]


def xcodebuild_test_without_building(
    *,
    xctestrun: Path,
    device: str,
    result: Path,
    only_testing: str,
    quiet: bool = False,
    launcher: str = XCRUN,
) -> list[str]:
    """Build one bounded xcodebuild test-without-building command.

    ``xcrun`` remains the default. A direct ``xcodebuild`` launcher is useful
    when an evidence schema records the exact command accepted by its checker.
    """
    if launcher == XCRUN:
        command = [XCRUN, "xcodebuild"]
    else:
        command = [launcher]
    if quiet:
        command.append("-quiet")
    command.extend(
        [
            "-xctestrun",
            str(xctestrun),
            "-destination",
            "id=" + device,
            "-resultBundlePath",
            str(result),
            "-parallel-testing-enabled",
            "NO",
            "-collect-test-diagnostics",
            "never",
            "-only-testing:" + only_testing,
            "test-without-building",
        ]
    )
    return command


def run_command(
    command: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one finite iOS SDK command with bounded combined output."""
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
        raise IosSdkError(
            f"command failed to start or finish: {' '.join(command)}"
        ) from error
    if check and result.returncode:
        raise IosSdkError(
            f"command failed (exit={result.returncode}): {' '.join(command)}; "
            f"{result.stdout[-1500:]}"
        )
    return result


def run_logged(
    command: list[str],
    *,
    cwd: Path,
    log: Path,
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one finite SDK command with stdin closed and a complete log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("xb") as output:
        return subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            timeout=timeout,
            check=check,
            text=True,
        )


def command_name(command: list[str]) -> str:
    """Return a bounded human label without depending on a host absolute path."""
    return " ".join(command[:2]) if len(command) >= 2 else os.fspath(command[0])


@dataclass(frozen=True)
class IosAppIdentity:
    """Exact native boot and official install/launch snapshots; not an installed hash.

    ``device`` is the requested physical UDID; ``device_identifier`` is the
    returned CoreDevice ID. Receipt digests freeze the raw supplied evidence.
    ``executable`` is the exact CoreDevice file URL, without container relaxation.
    All other native fields correspond to the producer's five-field protocol.
    """

    device: str
    device_identifier: str
    run_id: str
    bundle_id: str
    pid: int
    boot_id: str
    executable: str
    install_receipt_sha256: str
    launch_receipt_sha256: str


def _identity_json(path: Path, maximum: int) -> tuple[dict[str, Any], bytes]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise IosSdkError("duplicate iOS identity JSON key")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise IosSdkError("non-finite iOS identity JSON value")

    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
    ):
        raise IosSdkError(
            "missing, linked, nonregular or oversized iOS identity evidence"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise IosSdkError("iOS identity evidence changed type")
        raw = source.read(maximum + 1)
        finished = os.fstat(source.fileno())
    after = path.lstat()
    snapshots = {
        (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
            item.st_nlink,
        )
        for item in (before, opened, finished, after)
    }
    if len(snapshots) != 1 or len(raw) > maximum or len(raw) != before.st_size:
        raise IosSdkError("iOS identity evidence changed or is oversized")
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
    )
    if type(value) is not dict:
        raise IosSdkError("iOS identity evidence must be an object")
    return value, raw


def _identity_official(
    path: Path, command_type: str, device: str, arguments: list[str] | None = None
) -> tuple[dict[str, Any], str]:
    document, raw = _identity_json(path, 1024 * 1024)
    result = official_result(path, command_type, device)
    _, after = _identity_json(path, 1024 * 1024)
    if (
        raw != after
        or result != document.get("result")
        or (arguments is not None and document["info"]["arguments"] != arguments)
    ):
        raise IosSdkError("iOS official receipt changed or command arguments differ")
    return result, hashlib.sha256(raw).hexdigest()


def _identity_uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _identity_executable_path(value: str, *, file_url: bool) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise IosSdkError("invalid native executable identity")
    if file_url:
        parsed = urlsplit(value)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise IosSdkError("invalid CoreDevice executable URL")
        value = unquote(parsed.path, errors="strict")
    if (
        not value.startswith("/")
        or posixpath.normpath(value) != value
        or "\x00" in value
    ):
        raise IosSdkError("noncanonical native executable path")
    return "/private" + value if value.startswith("/var/") else value


def read_app_identity(
    *,
    device: str,
    run_id: str,
    bundle_id: str,
    install_receipt: Path,
    launch_receipt: Path,
    attempt: Path,
    expected_identity: IosAppIdentity | None,
    require_owner: Callable[[], None],
    cwd: Path,
    deadline: float,
) -> IosAppIdentity:
    """Inspect an already-running formal host using at least six SDK commands.

    Every command is bracketed by ``require_owner`` under the same absolute
    monotonic ``deadline`` (including on SDK failure). Only an exact pinned
    response with a null or other canonical UUID challenge may wait for the fresh
    challenge; SDK errors and malformed/foreign responses fail immediately. Each
    repeated inspection copy adds one command and retains its own destination,
    receipt and full output. A private, new challenge directory under ``attempt``
    retains all evidence. A repeated challenge is refused, not retried.
    ``expected_identity`` forbids re-selection on recheck. This function never
    controls App lifecycle, permissions or radio; container identity IPC is not
    radio authorization.
    Installation provenance and local artifact hashing belong to the mandatory
    callbacks in the host binding, not CoreDevice inventory or this native IPC.
    """
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("deadline must be a finite absolute monotonic time")
    if (
        not _identity_uuid(run_id)
        or type(device) is not str
        or not device.strip()
        or type(bundle_id) is not str
        or re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id) is None
        or not callable(require_owner)
    ):
        raise ValueError("explicit iOS device/run/App/owner identity is required")

    def budget() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("iOS App identity total deadline exceeded")
        return min(5.0, remaining)

    receipt_digests: tuple[str, str] | None = None

    def guard() -> None:
        budget()
        require_owner()
        if receipt_digests is not None:
            _, current_install = _identity_official(
                install_receipt, "devicectl.device.install.app", device
            )
            _, current_launch = _identity_official(
                launch_receipt, "devicectl.device.process.launch", device
            )
            if (current_install, current_launch) != receipt_digests:
                raise IosSdkError(
                    "iOS install/launch receipts changed during inspection"
                )
        budget()

    def sdk_timeout() -> int:
        value = int(budget())
        if value < 1:
            raise TimeoutError("insufficient iOS identity SDK deadline budget")
        return value

    guard()
    installed, install_digest = _identity_official(
        install_receipt, "devicectl.device.install.app", device
    )
    launched, launch_digest = _identity_official(
        launch_receipt, "devicectl.device.process.launch", device
    )
    launch_document, _ = _identity_json(launch_receipt, 1024 * 1024)
    arguments = launch_document["info"]["arguments"]
    flag = "--blehub-formal-identity-run-id"
    process = launched.get("process")
    rows = installed.get("installedApplications")
    if (
        type(rows) is not list
        or any(type(row) is not dict for row in rows)
        or type(process) is not dict
        or type(process.get("processIdentifier")) is not int
        or process["processIdentifier"] <= 0
        or type(process.get("executable")) is not str
        or launched["deviceIdentifier"] != installed["deviceIdentifier"]
        or arguments.count(bundle_id) != 1
        or arguments.count(flag) != 1
        or arguments[arguments.index(flag) + 1 :][:1] != [run_id]
        or "--payload-url" in arguments
        or any(
            type(argument) is not str or argument.startswith(flag + "=")
            for argument in arguments
        )
    ):
        raise IosSdkError("foreign/malformed iOS install or formal launch identity")
    matches = [row for row in rows if row.get("bundleID") == bundle_id]
    executable, pid = process["executable"], process["processIdentifier"]
    native_path = _identity_executable_path(executable, file_url=True)
    if (
        len(matches) != 1
        or matches[0].get("installationURL") != executable.rsplit("/", 1)[0] + "/"
    ):
        raise IosSdkError("iOS install/launch executable is not exact and unique")
    fixed = (
        device,
        installed["deviceIdentifier"],
        run_id,
        bundle_id,
        pid,
        executable,
        install_digest,
        launch_digest,
    )
    if expected_identity is not None and fixed != (
        expected_identity.device,
        expected_identity.device_identifier,
        expected_identity.run_id,
        expected_identity.bundle_id,
        expected_identity.pid,
        expected_identity.executable,
        expected_identity.install_receipt_sha256,
        expected_identity.launch_receipt_sha256,
    ):
        raise IosSdkError("pinned iOS install/launch identity changed")
    receipt_digests = (install_digest, launch_digest)
    guard()
    if attempt.is_symlink() or not attempt.is_dir():
        raise IosSdkError("existing private iOS identity attempt directory required")
    challenge = str(uuid.uuid4())
    evidence = attempt / ("ios-identity-" + challenge)
    evidence.mkdir()  # Exclusive: a challenge must never be reused, even after failure.

    def sdk(command: list[str]) -> dict[str, Any]:
        receipt = Path(command[command.index("--json-output") + 1])
        log = receipt.with_suffix(".log")
        guard()
        command[command.index("--timeout") + 1] = str(sdk_timeout())
        try:
            result = run_command(command, cwd=cwd, timeout=budget(), check=False)
        except Exception as error:
            try:
                guard()
            except Exception as boundary:  # noqa: BLE001 -- preserve primary SDK failure and record guard failure.
                error.add_note(f"iOS post-command owner/deadline failure: {boundary}")
            log.write_text(
                str(error) + "\n" + str(getattr(error.__cause__, "stdout", "")),
                encoding="utf-8",
            )
            raise
        try:
            guard()
        finally:
            log.write_text(result.stdout, encoding="utf-8")
        if result.returncode:
            raise IosSdkError(
                f"iOS identity SDK failed (exit={result.returncode}); log={log}"
            )
        value, _ = _identity_official(
            receipt, ".".join(command[1:5]), device, command[1:]
        )
        if value["deviceIdentifier"] != installed["deviceIdentifier"]:
            raise IosSdkError("iOS identity SDK returned a foreign CoreDevice ID")
        return value

    def processes(name: str) -> None:
        result = sdk(
            devicectl_processes(device, evidence / name, timeout=sdk_timeout())
        )
        current = result.get("runningProcesses")
        if type(current) is not list or any(
            type(row) is not dict
            or type(row.get("processIdentifier")) is not int
            or row["processIdentifier"] <= 0
            for row in current
        ):
            raise IosSdkError("malformed current iOS process list")
        by_pid = [row for row in current if row["processIdentifier"] == pid]
        by_executable = [row for row in current if row.get("executable") == executable]
        if len(by_pid) != 1 or len(by_executable) != 1 or by_pid[0] != by_executable[0]:
            raise IosSdkError("pinned iOS process missing, ambiguous or changed")

    remote = f"Documents/blehub-formal-identity/{run_id}/"

    def pull(
        filename: str, receipt: str, *, local_name: str | None = None
    ) -> dict[str, Any]:
        destination = evidence / (local_name or filename)
        sdk(
            devicectl_copy_from(
                device,
                remote + filename,
                bundle_id,
                destination,
                evidence / receipt,
                timeout=sdk_timeout(),
            )
        )
        value, _ = _identity_json(destination, 4096)
        return value

    def native(value: dict[str, Any]) -> str:
        if (
            set(value) != {"run_id", "boot_id", "pid", "bundle_id", "executable"}
            or value["run_id"] != run_id
            or value["bundle_id"] != bundle_id
            or type(value["pid"]) is not int
            or value["pid"] != pid
            or not _identity_uuid(value["boot_id"])
            or _identity_executable_path(value["executable"], file_url=False)
            != native_path
        ):
            raise IosSdkError("foreign/malformed native iOS process identity")
        return value["boot_id"]

    processes("01-processes-before.json")
    identity = pull("native-identity.json", "02-identity-copy.json")
    boot_id = native(identity)
    if expected_identity is not None and boot_id != expected_identity.boot_id:
        raise IosSdkError("pinned iOS native boot changed, even if PID was reused")
    request = evidence / "identity-request.json"
    with request.open("x", encoding="utf-8") as output:
        json.dump({"boot_id": boot_id, "challenge": challenge}, output, sort_keys=True)
    sdk(
        devicectl_copy_to(
            device,
            request,
            remote + request.name,
            bundle_id,
            evidence / "03-request-copy.json",
            timeout=sdk_timeout(),
        )
    )
    sdk(
        devicectl_post_notification(
            device,
            f"dev.blehub.formal-rn.{run_id}.{boot_id}.inspect",
            evidence / "04-post.json",
            timeout=sdk_timeout(),
        )
    )
    inspection_number = 0
    while True:
        inspection_number += 1
        inspection = pull(
            "native-inspection.json",
            f"05-inspection-{inspection_number:04d}-copy.json",
            local_name=f"native-inspection-{inspection_number:04d}.json",
        )
        if (
            set(inspection) != {"identity", "challenge"}
            or type(inspection["identity"]) is not dict
            or inspection["identity"] != identity
        ):
            raise IosSdkError("malformed or foreign native iOS inspection response")
        native(inspection["identity"])
        response_challenge = inspection["challenge"]
        if response_challenge is not None and not _identity_uuid(response_challenge):
            raise IosSdkError("malformed native iOS inspection challenge")
        if response_challenge == challenge:
            break
        # Posting Darwin notification does not acknowledge responder completion.
        guard()
        time.sleep(min(0.05, budget()))
        guard()
    processes("06-processes-after.json")
    guard()
    return IosAppIdentity(
        device,
        installed["deviceIdentifier"],
        run_id,
        bundle_id,
        pid,
        boot_id,
        executable,
        install_digest,
        launch_digest,
    )
