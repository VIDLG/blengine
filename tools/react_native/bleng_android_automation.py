"""Read-only preparation inventory for the formal Android Appium host.

This is deliberately not a session owner or an admission receipt. It inspects
already-provisioned helper APKs under the actual phone lease, without installing,
root probing, clearing old sessions, changing settings or repairing automation.
A verified inventory still needs live-session ownership and settings finalization.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from traceback import format_exception_only

from tools.android.android_appium import uiautomator2_support_apks
from tools.android.state_control import AndroidRadioController

from . import android_sdk
from .capture_io import publish_new_json

SETTINGS_PACKAGE = "io.appium.settings"
SERVER_PACKAGE = "io.appium.uiautomator2.server"
TEST_PACKAGE = SERVER_PACKAGE + ".test"
HELPER_PACKAGES = (SETTINGS_PACKAGE, SERVER_PACKAGE, TEST_PACKAGE)
INSTRUMENTATION_COMPONENT = TEST_PACKAGE + "/androidx.test.runner.AndroidJUnitRunner"
HIDDEN_API_POLICIES = (
    "hidden_api_policy_pre_p_apps",
    "hidden_api_policy_p_apps",
    "hidden_api_policy",
)
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_APK_BYTES = 128 * 1024 * 1024


def parse_adb_forward_list(output: str) -> list[list[str]]:
    """Parse adb rows, ignoring only empty trailing CLI terminator lines."""
    lines = output.splitlines()
    while lines and not lines[-1]:
        lines.pop()
    rows = []
    for line in lines:
        fields = line.split()
        if len(fields) != 3 or fields in rows:
            raise RuntimeError("malformed/duplicate adb forwarding inventory")
        for endpoint in fields[1:]:
            if endpoint.startswith("tcp:"):
                port = endpoint.removeprefix("tcp:")
                if re.fullmatch(r"[1-9][0-9]{0,4}", port) is None or int(port) > 65535:
                    raise RuntimeError(
                        "noncanonical/invalid adb TCP forwarding endpoint"
                    )
        rows.append(fields)
    return sorted(rows)


def parse_mock_location_scopes(value: str) -> dict[str, str]:
    """Preserve UID/package scopes; do not guess precedence for conflicting modes."""
    value = value.strip()
    if value == "No operations.":
        return {"default": "default"}
    default = re.fullmatch(
        r"No operations\.\nDefault mode: (allow|deny|ignore|default)", value
    )
    if default is not None:
        return {"default": default[1]}
    modes = {}
    for line in value.splitlines():
        match = re.fullmatch(
            r"(Uid mode: )?(?:android:mock_location|MOCK_LOCATION): "
            r"(allow|deny|ignore|default)(?:;[^\r\n]*)?",
            line,
        )
        if match is None or bool(match[1]) in modes:
            raise RuntimeError(
                "unsupported/ambiguous helper mock-location policy readback"
            )
        modes[bool(match[1])] = match[2]
    if not modes or len(set(modes.values())) != 1:
        raise RuntimeError(
            "conflicting UID/package mock-location modes; effective mode unverified"
        )
    return {"uid" if uid else "package": mode for uid, mode in modes.items()}


def require_mock_location_restored(original: str, observed: str) -> None:
    """Check mode/scope restoration, not disappearance of Android's op record.

    Setting a previously absent/default-deny operation can leave a package deny
    entry after restoration. Accept only that observed representation change;
    never normalize UID scopes, grants, or the MODE_DEFAULT permission fallback.
    Keep both raw observations in the caller's evidence.
    """
    expected = parse_mock_location_scopes(original)
    current = parse_mock_location_scopes(observed)
    if current == expected:
        return
    if expected == {"default": "deny"} and current == {"package": "deny"}:
        return
    raise RuntimeError("helper mock-location mode/scope was not restored")


def inspect_prepared_android_automation(
    *,
    phone: AndroidRadioController,
    appium_home: Path,
    expected_apk_sha256: dict[str, str],
    system_port: int,
    output: Path,
    cwd: Path,
    deadline: float,
) -> Path:
    """Publish a passive inventory; the report is never a session authority token."""
    path, _ = _collect_prepared_android_automation(
        phone=phone,
        appium_home=appium_home,
        expected_apk_sha256=expected_apk_sha256,
        system_port=system_port,
        output=output,
        cwd=cwd,
        deadline=deadline,
    )
    return path


def hash_helper_apk(path: Path, *, require_current: Callable[[], None]) -> str:
    """Hash one bounded stable helper APK under the caller's deadline/authority."""
    require_current()
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= MAX_APK_BYTES
    ):
        raise RuntimeError("helper APK is linked, nonregular, empty or oversized")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError("helper APK changed type while opening")
        while chunk := source.read(1024 * 1024):
            require_current()
            size += len(chunk)
            if size > MAX_APK_BYTES:
                raise RuntimeError("helper APK grew beyond the size limit")
            digest.update(chunk)
        finished = os.fstat(source.fileno())
    after = path.lstat()
    signatures = {
        (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        for info in (before, opened, finished, after)
    }
    if len(signatures) != 1 or size != before.st_size:
        raise RuntimeError("helper APK changed during inspection")
    require_current()
    return digest.hexdigest()


def _collect_prepared_android_automation(
    *,
    phone: AndroidRadioController,
    appium_home: Path,
    expected_apk_sha256: dict[str, str],
    system_port: int,
    output: Path,
    cwd: Path,
    deadline: float,
) -> tuple[Path, dict]:
    """Collect live observations for an in-process owner, also publishing a report.

    Inputs name an existing App-only phone lease, a complete local helper triplet
    and its reviewed SHA256 values, and the proposed host forwarding port. Every
    SDK command has the same absolute deadline, a five-second cap and pre/post
    lease/local-artifact checks. Reverse reads detect observed installation, boot,
    process, forwarding and policy drift; this is not an atomic device snapshot.
    The fresh output directory remains as failed evidence after an admitted error.
    The caller retains every resource, including phone release/quarantine policy.
    """
    if type(phone) is not AndroidRadioController:
        raise TypeError("an actual Android phone controller is required")
    if type(system_port) is not int or not 1 <= system_port <= 65535:
        raise ValueError("system_port must be an explicit integer in 1..65535")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("finite absolute Android automation deadline required")
    if not all(isinstance(path, Path) for path in (appium_home, output, cwd)):
        raise TypeError("explicit Appium home/output/cwd paths required")
    if (
        type(expected_apk_sha256) is not dict
        or set(expected_apk_sha256) != set(HELPER_PACKAGES)
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in expected_apk_sha256.values()
        )
    ):
        raise ValueError(
            "exactly three explicit lowercase helper SHA256 values required"
        )
    expected = dict(expected_apk_sha256)
    device, run_id = phone.device, phone.run_id
    if (
        type(device) is not str
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", device) is None
        or type(run_id) is not str
        or str(uuid.UUID(run_id)) != run_id
    ):
        raise ValueError("canonical explicit phone/run identity required")
    if device.startswith("emulator-"):
        raise ValueError("formal Android automation requires a physical phone")

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Android automation inventory deadline exceeded")
        return budget

    def require_phone() -> None:
        remaining()
        if (
            phone.device != device
            or phone.run_id != run_id
            or phone.document.get("device") != device
            or phone.document.get("run_id") != run_id
            or phone.document.get("actions") != []
            or phone.initial_state is not None
            or phone.last_state is not None
        ):
            raise RuntimeError("Android App-only phone/run ownership changed")
        phone.require_owner()
        remaining()

    require_phone()
    paths = uiautomator2_support_apks(appium_home)

    def guard() -> None:
        require_phone()
        if uiautomator2_support_apks(appium_home) != paths:
            raise RuntimeError("local helper APK selection changed")
        for package, path in zip(HELPER_PACKAGES, paths, strict=True):
            if (
                hash_helper_apk(path, require_current=require_phone)
                != expected[package]
            ):
                raise RuntimeError(f"local helper APK SHA256 changed: {package}")
        require_phone()

    guard()  # Finish local validation before creating evidence or issuing SDK I/O.
    output = output.absolute()
    output.mkdir(mode=0o700)
    result_path = output / "result.json"
    document = {
        "schema_version": 1,
        "scope": "formal-rn-android-automation-inventory",
        "device": device,
        "run_id": run_id,
        "requested_system_port": system_port,
        "status": "failed",
        "automation_admission": "pending",
        "primary_error": None,
        "helpers": {},
        "remaining_requirements": [
            "live-session owner binding and same-scope instrumentation/forward ownership",
            "settings/AppOps lifecycle baseline and verified platform finalizer",
            "active instrumentation and Settings service scope, not proved by process inventory",
            "driver resolution/provenance and complete host evidence bundle",
        ],
    }
    sequence = 0

    def describe(error: BaseException) -> str:
        return "".join(format_exception_only(error, show_group=True)).strip()

    def command(*arguments: str) -> str:
        nonlocal sequence
        guard()
        argv = android_sdk.adb_command(device, *arguments)
        guard()  # Tool resolution also consumes the shared budget.
        sequence += 1
        receipt = output / f"{sequence:03d}-command.json"
        record = {"argv": argv, "returncode": None, "stdout": "", "error": None}
        primary: BaseException | None = None
        try:
            completed = android_sdk.run_command(
                argv, cwd=cwd, timeout=min(5.0, remaining()), check=False
            )
            record["returncode"] = completed.returncode
            if not isinstance(completed.stdout, str):
                raise android_sdk.AndroidSdkError(
                    "invalid Android inventory SDK output"
                )
            raw = completed.stdout.encode()
            record["stdout"] = raw[:MAX_OUTPUT_BYTES].decode(errors="replace")
            if completed.returncode:
                raise android_sdk.AndroidSdkError(
                    f"Android inventory SDK failed (exit={completed.returncode}): "
                    + completed.stdout[-1500:]
                )
            if len(raw) > MAX_OUTPUT_BYTES:
                raise android_sdk.AndroidSdkError(
                    "oversized Android inventory SDK output"
                )
        except BaseException as error:  # noqa: BLE001 -- retain SDK failures and interruptions with their evidence.
            primary = error
        try:
            guard()
        except BaseException as boundary:  # noqa: BLE001 -- do not replace the SDK cause with lease/hash failure.
            if primary is None:
                primary = boundary
            else:
                primary.add_note(
                    f"Android post-command owner/artifact boundary: {describe(boundary)}"
                )
        if primary is not None:
            record["error"] = describe(primary)
        try:
            publish_new_json(receipt, record)
        except BaseException as publication:
            if primary is None:
                raise
            primary.add_note(
                f"Android command receipt publication: {describe(publication)}"
            )
        if primary is not None:
            raise primary
        guard()
        return completed.stdout

    def boot() -> str:
        value = command("shell", "cat", "/proc/sys/kernel/random/boot_id")
        if (
            re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n", value)
            is None
        ):
            raise RuntimeError("invalid Android OS boot identity")
        return value.strip()

    def require_user() -> None:
        if command("shell", "am", "get-current-user").strip() != "0":
            raise RuntimeError("Android automation inventory requires current user 0")
        users = command("shell", "pm", "list", "users").splitlines()
        if (
            len(users) != 2
            or users[0].strip() != "Users:"
            or re.fullmatch(r"\s*UserInfo\{0:[^\n{}]+\}(?: running)?\s*", users[1])
            is None
        ):
            raise RuntimeError("Android automation inventory requires a single user 0")

    def package_uids() -> dict[str, int]:
        packages = {}
        for line in command(
            "shell", "pm", "list", "packages", "-U", "--user", "0"
        ).splitlines():
            match = re.fullmatch(r"package:([A-Za-z0-9_.]+) uid:([0-9]+)", line)
            if match is None or match[1] in packages:
                raise RuntimeError("malformed Android package UID inventory")
            packages[match[1]] = int(match[2])
        for package in HELPER_PACKAGES:
            uid = packages.get(package)
            if (
                uid is None
                or not 10000 <= uid < 100000
                or list(packages.values()).count(uid) != 1
            ):
                raise RuntimeError(f"missing/shared/non-primary helper UID: {package}")
        return {package: packages[package] for package in HELPER_PACKAGES}

    def helper_paths(pinned: dict[str, str] | None = None) -> dict[str, str]:
        installed = {}
        for package in HELPER_PACKAGES:
            value = command("shell", "pm", "path", package)
            match = re.fullmatch(
                r"package:(/data/app/[A-Za-z0-9_./=+~-]+/base\.apk)\n", value
            )
            if match is None or any(
                part in ("", ".", "..") for part in match[1].split("/")[1:]
            ):
                raise RuntimeError(
                    f"missing/split/noncanonical installed helper: {package}"
                )
            path = match[1]
            if pinned is not None and path != pinned[package]:
                raise RuntimeError(f"installed helper path changed: {package}")
            if command("shell", "sha256sum", path) != f"{expected[package]}  {path}\n":
                raise RuntimeError(
                    f"installed helper SHA256 differs from candidate: {package}"
                )
            installed[package] = path
        return installed

    def instrumentation() -> None:
        registrations = []
        for line in command("shell", "pm", "list", "instrumentation").splitlines():
            match = re.fullmatch(
                r"instrumentation:(\S+) \(target=([A-Za-z0-9_.]+)\)", line
            )
            if match is None or match[1] in [
                component for component, _ in registrations
            ]:
                raise RuntimeError("malformed/duplicate instrumentation registration")
            registrations.append((match[1], match[2]))
        if (INSTRUMENTATION_COMPONENT, SERVER_PACKAGE) not in registrations:
            raise RuntimeError(
                "expected UiAutomator2 instrumentation is not registered"
            )

    def idle_processes(uids: dict[str, int]) -> list[dict]:
        lines = command("shell", "ps", "-A", "-o", "PID,UID,ARGS").splitlines()
        if not lines or lines[0].split() != ["PID", "UID", "ARGS"]:
            raise RuntimeError("unsupported Android process inventory header")
        seen, settings = set(), []
        for line in lines[1:]:
            fields = line.split(maxsplit=2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise RuntimeError("malformed Android process inventory")
            pid, uid, arguments = int(fields[0]), int(fields[1]), fields[2]
            if not 0 < pid <= 2**31 - 1 or pid in seen:
                raise RuntimeError("duplicate/invalid Android process identity")
            seen.add(pid)
            name = arguments.split()[0]
            if (
                name.rsplit("/", 1)[-1] == "uiautomator"
                or "com.android.commands.uiautomator.Launcher" in arguments.split()
            ):
                raise RuntimeError("unknown pre-existing uiautomator process")
            related = uid in uids.values() or any(
                name == p or name.startswith(p + ":") for p in HELPER_PACKAGES
            )
            if not related:
                continue
            if uid != uids[SETTINGS_PACKAGE] or name != SETTINGS_PACKAGE:
                raise RuntimeError(
                    "unknown helper/UID process; refusing automation takeover"
                )
            settings.append({"pid": pid, "uid": uid, "args": arguments})
        if len(settings) > 1:
            raise RuntimeError("ambiguous Settings helper process inventory")
        return settings

    def forwards() -> list[list[str]]:
        rows = parse_adb_forward_list(command("forward", "--list"))
        for serial, local, remote in rows:
            if local == f"tcp:{system_port}" or (
                serial == device and remote in {"tcp:6790", "tcp:7810"}
            ):
                raise RuntimeError(
                    "pre-existing automation forwarding collision; refusing takeover"
                )
        return rows

    def policies() -> dict[str, str]:
        values = {}
        for key in HIDDEN_API_POLICIES:
            value = command("shell", "settings", "get", "global", key).strip()
            if value not in {"null", "-1", "0", "1", "2"}:
                raise RuntimeError("unsupported hidden-API policy readback")
            values[key] = value
        return values

    def mock_location() -> dict[str, str]:
        value = command(
            "shell", "cmd", "appops", "get", SETTINGS_PACKAGE, "android:mock_location"
        ).strip()
        return parse_mock_location_scopes(value)

    primary: BaseException | None = None
    try:
        if command("get-state").strip() != "device":
            raise RuntimeError("explicit Android phone is unavailable")
        identity = boot()
        require_user()
        uids = package_uids()
        installed = helper_paths()
        instrumentation()
        settings = idle_processes(uids)
        forwarding = forwards()
        policy = policies()
        location = mock_location()
        # Re-read all observed policy/identity boundaries, not just a fresh boot ID.
        require_user()
        instrumentation()
        if (
            policies() != policy
            or mock_location() != location
            or package_uids() != uids
        ):
            raise RuntimeError("Android helper policy/UID changed during inspection")
        if helper_paths(installed) != installed:
            raise RuntimeError("installed helper path changed during inspection")
        if (
            idle_processes(uids) != settings
            or forwards() != forwarding
            or boot() != identity
        ):
            raise RuntimeError(
                "Android automation boot/process/forwarding changed during inspection"
            )
        guard()
        document.update(
            status="inventory-verified",
            boot_id=identity,
            helpers={
                p: {
                    "installed_path": installed[p],
                    "sha256": expected[p],
                    "uid": uids[p],
                }
                for p in HELPER_PACKAGES
            },
            settings_processes=settings,
            forwardings=forwarding,
            observed_hidden_api_policy=policy,
            observed_mock_location_mode=next(iter(location.values())),
            observed_mock_location_scopes=location,
        )
    except BaseException as error:  # noqa: BLE001 -- emit failed evidence without claiming ownership or cleaning devices.
        primary = error
        document["primary_error"] = describe(error)
    if primary is not None:
        try:
            publish_new_json(result_path, document)
        except BaseException as publication:  # noqa: BLE001 -- preserve the inspection/SDK cause and external evidence.
            primary.add_note(
                f"Android inventory result publication: {describe(publication)}"
            )
        raise primary

    published: list[tuple[int, int]] = []
    digest = publish_new_json(result_path, document, on_commit=published.append)
    try:
        guard()  # Publication itself must not extend the deadline or revive a lost lease.
    except BaseException as boundary:
        document.update(status="failed", primary_error=describe(boundary))
        try:
            info = result_path.lstat()
            if (
                published != [(info.st_dev, info.st_ino)]
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_OUTPUT_BYTES
                or hashlib.sha256(result_path.read_bytes()).hexdigest() != digest
            ):
                raise RuntimeError(
                    "inventory result was replaced or modified; refusing retraction"
                )
            current = result_path.lstat()
            if (current.st_dev, current.st_ino) != published[0]:
                raise RuntimeError(
                    "inventory result identity changed before retraction"
                )
            result_path.unlink()
            publish_new_json(result_path, document)
        except BaseException as publication:  # noqa: BLE001 -- late failures must survive rollback/publication failure.
            boundary.add_note(
                f"Android inventory result retraction/publication: {describe(publication)}"
            )
        raise
    return result_path, document
