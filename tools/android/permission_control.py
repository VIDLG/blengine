"""Leased, bounded Android permission changes and independent OS death evidence.

Only API >= 31, current user 0, and com.blehubnitrohost are supported. Pass the
existing AndroidRadioController.require_owner as phone_guard; this module owns
no lease, radio, process launcher, host kill, or application event semantics.
ApplicationExitInfo's four-line dump is from the installed android-36 source
(android/app/ApplicationExitInfo.java, dump), not its toString representation.
Unknown ROM formats and permission revocation without process death fail closed.
"""

from __future__ import annotations

import hashlib
import math
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from tests.peripherals.nrf52840.cancellation_control import write_document
from tools.android_toolchain import adb_executable
from tools.react_native import android_sdk

PACKAGE = "com.blehubnitrohost"
SCAN = "android.permission.BLUETOOTH_SCAN"
CONNECT = "android.permission.BLUETOOTH_CONNECT"
PERMISSIONS = (SCAN, CONNECT)
ROOT_ID_ARGS = ("su", "-c", "id")
MAX_OUTPUT_BYTES = 512 * 1024
MAX_RECEIPTS = 256
COMMAND_SECONDS = 5.0
ACTION_SECONDS = 15.0
POLL_SECONDS = 0.1


class AndroidProcessIdentity(TypedDict):
    """Caller-pinned old instance; integers, not strings, in device clock domain.

    pid/uid are the Linux PID and package UID (user 0 application UID).
    user_id must be 0; process_name must be PACKAGE (no remote processes).
    start_ticks is /proc/PID/stat field 22, not elapsed milliseconds.
    native_started_unix_ms is the old native process's Unix-ms startup timestamp,
    not a host receipt timestamp or a JS reload timestamp. The caller owns capture.
    """

    pid: int
    uid: int
    user_id: int
    process_name: str
    start_ticks: int
    native_started_unix_ms: int


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def permission_mutation_arguments(
    verb: str, permission: str, *, use_root: bool
) -> tuple[str, ...]:
    """Encode the fixed user0 Bluetooth mutation, never arbitrary shell input."""
    _require(
        verb in ("grant", "revoke") and permission in PERMISSIONS,
        "invalid permission mutation",
    )
    args = ("pm", verb, "--user", "0", PACKAGE, permission)
    return ("su", "-c", shlex.quote(" ".join(args))) if use_root else args


def verify_package_uid_listing(output: str, uid: int) -> None:
    """Select the exact package from Android's substring-filtered package list.

    Instrumentation packages can also match PACKAGE. All rows must remain valid,
    distinct filtered package identities; none may substitute for the exact app.
    """
    packages: dict[str, int] = {}
    for line in output.splitlines():
        match = re.fullmatch(r"package:([A-Za-z][A-Za-z0-9_.]*) uid:(\d+)", line)
        _require(match is not None, "malformed package/user0 UID listing")
        assert match is not None
        name, value = match[1], int(match[2])
        _require(
            PACKAGE in name and name not in packages and 10000 <= value < 100000,
            "foreign/duplicate/non-user0 package UID listing",
        )
        packages[name] = value
    _require(
        packages.get(PACKAGE) == uid,
        "package/user0 UID listing mismatch or ambiguity",
    )


def _section(lines: list[str], pattern: str) -> list[str]:
    matches = [i for i, line in enumerate(lines) if re.fullmatch(pattern, line)]
    _require(len(matches) == 1, f"missing/duplicate section: {pattern}")
    start = matches[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        end += 1
    return lines[start + 1 : end]


def parse_permissions(output: str) -> tuple[int, dict[str, bool]]:
    """Read exactly one installed User 0 runtime section of the exact package.

    Requested/install permissions and other users are deliberately not evidence.
    Duplicate grants, UIDs, sections, missing permissions and malformed flags fail.
    """
    package = _section(
        output.splitlines(), rf"\s*Package \[{re.escape(PACKAGE)}\] \([^\n]+\):"
    )
    uids = re.findall(r"(?m)^\s*(?:userId|appId)=(\d+)\s*$", "\n".join(package))
    _require(len(uids) == 1, "missing/duplicate package UID")
    uid = int(uids[0])
    _require(10000 <= uid < 100000, "package UID is not a user0 application UID")
    users = [line for line in package if re.match(r"\s*User 0:", line)]
    _require(
        len(users) == 1 and re.findall(r"\binstalled=(\S+)", users[0]) == ["true"],
        "missing/duplicate/uninstalled User 0",
    )
    user = _section(package, r"\s*User 0:.*")
    runtime = _section(user, r"\s*runtime permissions:")
    grants: dict[str, bool] = {}
    for permission in PERMISSIONS:
        lines = [line for line in runtime if line.strip().startswith(permission + ":")]
        _require(len(lines) == 1, f"missing/duplicate runtime grant: {permission}")
        match = re.fullmatch(
            rf"\s*{re.escape(permission)}: granted=(true|false)"
            r"(?:, flags=\[[^\r\n]*\])?\s*",
            lines[0],
        )
        _require(match is not None, f"malformed runtime grant: {permission}")
        assert match is not None
        grants[permission] = match[1] == "true"
    return uid, grants


_EXIT = re.compile(
    r"^[ \t]*ApplicationExitInfo #\d+:\n"
    r"[ \t]+timestamp=(?P<timestamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})"
    r" pid=(?P<pid>\d+) realUid=(?P<real_uid>\d+)"
    r" packageUid=(?P<package_uid>\d+) definingUid=(?P<defining_uid>\d+)"
    r" user=(?P<user_id>\d+)\n"
    r"[ \t]+process=(?P<process_name>\S+) reason=(?P<reason>\d+)"
    r" \((?P<reason_name>[^\n]*)\) subreason=\d+ \([^\n]*\) status=-?\d+\n"
    r"[ \t]+importance=\d+ pss=[^\n]+ rss=[^\n]+ description=[^\n]*"
    r" state=[^\n]+ trace=[^\n]+$",
    re.MULTILINE,
)


def parse_exit_history(output: str, utc_offset: str) -> list[dict[str, Any]]:
    """Parse complete android-36 dump records; never accept a partial record.

    utc_offset is the device's date +%z, not the host's timezone. Fingerprints
    exclude dump sequence indices and mutable description/memory fields.
    A validated optional leading TIME banner is metadata, not an event clock.
    """
    _require(bool(re.fullmatch(r"[+-]\d{4}", utc_offset)), "invalid device UTC offset")
    lines = output.splitlines()
    if lines:
        banner = re.fullmatch(
            r"---- TIME ([0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) ----",
            lines[0],
        )
        if banner is not None:
            try:
                datetime.strptime(banner[1], "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError as error:
                raise RuntimeError("invalid exit-info TIME banner timestamp") from error
            lines = lines[1:]
    text = "\n".join(lines)
    _require(
        text.splitlines()[:1]
        == ["ACTIVITY MANAGER PROCESS EXIT INFO (dumpsys activity exit-info)"],
        "unknown exit-info dump header",
    )
    packages = re.findall(r"(?m)^\s*package: (\S+)\s*$", text)
    _require(all(p == PACKAGE for p in packages), "foreign exit-info package")
    matches = list(_EXIT.finditer(text))
    _require(
        text.count("ApplicationExitInfo") == len(matches),
        "missing/ambiguous/malformed ApplicationExitInfo record",
    )
    remainder = _EXIT.sub("", text)
    for line in remainder.splitlines()[1:]:
        _require(
            not line.strip()
            or re.fullmatch(
                r"\s*(?:Last Timestamp of Persistence Into Persistent Storage: .+"
                rf"|package: {re.escape(PACKAGE)}"
                r"|Historical Process Exit for (?:userId|uid)=\d+:?)",
                line,
            )
            is not None,
            "unknown/incomplete exit-info history format",
        )
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        value: dict[str, Any] = match.groupdict()
        for key in (
            "pid",
            "real_uid",
            "package_uid",
            "defining_uid",
            "user_id",
            "reason",
        ):
            value[key] = int(value[key])
        value["timestamp_unix_ms"] = round(
            datetime.strptime(
                value["timestamp"] + utc_offset, "%Y-%m-%d %H:%M:%S.%f%z"
            ).timestamp()
            * 1000
        )
        identity = tuple(
            value[k]
            for k in (
                "timestamp_unix_ms",
                "pid",
                "real_uid",
                "package_uid",
                "user_id",
                "process_name",
            )
        )
        value["fingerprint"] = _digest(repr(identity))
        _require(value["fingerprint"] not in seen, "duplicate exit record identity")
        seen.add(value["fingerprint"])
        records.append(value)
    return records


def _start_ticks(output: str, pid: int) -> int:
    match = re.fullmatch(r"(\d+) \([^\n]*\) ([^\n]+)\s*", output.strip())
    _require(match is not None and int(match[1]) == pid, "invalid /proc stat PID")
    assert match is not None
    fields = match[2].split()
    _require(
        len(fields) >= 20 and fields[19].isdecimal() and int(fields[19]) > 0,
        "invalid /proc start_ticks",
    )
    return int(fields[19])


def _deadline(timeout: float, maximum: float) -> float:
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= maximum
    ):
        raise ValueError(f"timeout must be finite and in (0, {maximum}] seconds")
    return time.monotonic() + timeout


class AndroidPermissionController:
    """Single-use permission evidence under a caller-owned phone lease.

    initial -> [deny_startup -> grant(label=...)] ->
    [revoke_connected -> await_permission_death -> grant(label=...)] -> finish.
    grant may also be used between independent caller-owned startup stages.
    No grants during an unresolved death gate, except explicit finish recovery.
    finish/finalize must run before the caller releases its phone lease.
    """

    def __init__(
        self,
        device: str,
        run_id: str,
        evidence_path: Path,
        *,
        phone_guard: Callable[[], None],
        use_root: bool = False,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", device):
            raise ValueError("an explicit canonical Android serial is required")
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("run_id must be a canonical UUID")
        self.device, self.run_id, self.path = device, run_id, evidence_path
        _require(type(use_root) is bool, "use_root must be boolean")
        self.phone_guard = phone_guard
        self.use_root = use_root
        self.uid: int | None = None
        self.initial_grants: dict[str, bool] | None = None
        self.baseline: set[str] = set()
        self.utc_offset: str | None = None
        self.old_identity: AndroidProcessIdentity | None = None
        self.revoke_time_ms: int | None = None
        self.revoke_monotonic: float | None = None
        self.revoke_confirmed = False
        self.death_confirmed = False
        self.initialized = False
        self.started = False
        self.finished = False
        self.document: dict[str, Any] = {
            "schema_version": 1,
            "platform": "android",
            "device": device,
            "run_id": run_id,
            "package": PACKAGE,
            "user_id": 0,
            "permission_command_mode": "root" if use_root else "shell",
            "status": "running",
            "uid": None,
            "initial_grants": None,
            "final_grants": None,
            "baseline_exit_records": [],
            "actions": [],
            "primary_error": None,
            "cleanup_errors": [],
        }

    def _publish(self) -> None:
        write_document(self.path, self.document)

    @contextmanager
    def _action(
        self,
        label: str,
        timeout: float = ACTION_SECONDS,
        *,
        maximum: float = ACTION_SECONDS,
    ) -> Iterator[tuple[dict[str, Any], float]]:
        deadline = _deadline(timeout, maximum)
        _require(not self.finished, "permission controller already finished")
        action: dict[str, Any] = {
            "label": label,
            "started_monotonic": time.monotonic(),
            "deadline_monotonic": deadline,
            "status": "running",
            "receipts": [],
        }
        self.document["actions"].append(action)
        failure: BaseException | None = None
        try:
            self.phone_guard()
            yield action, deadline
            _require(
                time.monotonic() < action["deadline_monotonic"],
                "permission action returned after deadline",
            )
            action["status"] = "confirmed"
        except BaseException as error:
            failure = error
            action.update(status="failed", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            action["finished_monotonic"] = time.monotonic()
            try:
                self._publish()
            except (OSError, ValueError, RuntimeError) as publication:
                action.update(status="failed", publication_error=str(publication))
                if failure is None:
                    raise
                failure.add_note(f"permission action publication failed: {publication}")

    def _command(
        self, action: dict[str, Any], deadline: float, *args: str, check: bool = True
    ) -> tuple[int, str]:
        _require(
            sum(len(a["receipts"]) for a in self.document["actions"]) < MAX_RECEIPTS,
            "permission journal receipt limit exceeded",
        )
        command = [str(adb_executable()), "-s", self.device, "shell", *args]
        receipt: dict[str, Any] = {
            "command": command,
            "started_monotonic": time.monotonic(),
        }
        action["receipts"].append(receipt)
        try:
            self._publish()  # Intent is durable before any mutation attempt.
            self.phone_guard()  # Also immediately guards each pm mutation.
            remaining = deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                raise TimeoutError("permission deadline exhausted before command")
            receipt["timeout_seconds"] = min(COMMAND_SECONDS, remaining)
            result = android_sdk.run_command(
                command, timeout=receipt["timeout_seconds"], check=False
            )
            receipt["returncode"] = result.returncode
            self._output(receipt, result.stdout, complete=True)
            if time.monotonic() >= deadline:
                raise TimeoutError("permission command returned after deadline")
            _require(
                not check or result.returncode == 0,
                f"Android permission command failed (exit={result.returncode}): {result.stdout.strip()[-800:]}",
            )
            return result.returncode, result.stdout
        except BaseException as error:
            receipt["error"] = f"{type(error).__name__}: {error}"
            cause = (
                error
                if isinstance(error, subprocess.TimeoutExpired)
                else error.__cause__
            )
            if (
                isinstance(cause, subprocess.TimeoutExpired)
                and cause.stdout is not None
            ):
                output = cause.stdout
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                self._output(receipt, output, complete=False)
            raise
        finally:
            receipt["finished_monotonic"] = time.monotonic()

    @staticmethod
    def _output(receipt: dict[str, Any], output: str, *, complete: bool) -> None:
        size = len(output.encode("utf-8"))
        receipt.update(
            stdout_bytes=size, stdout_sha256=_digest(output), stdout_complete=complete
        )
        if size > MAX_OUTPUT_BYTES:
            receipt.update(stdout_complete=False, output_overflow=True)
            raise RuntimeError(
                "Android stdout exceeds evidence bound; no truncated text retained"
            )
        receipt["stdout"] = output

    def _permissions(self, action: dict[str, Any], deadline: float) -> dict[str, bool]:
        _, current = self._command(action, deadline, "am", "get-current-user")
        _require(current.strip() == "0", "only current Android user0 is supported")
        _, output = self._command(action, deadline, "dumpsys", "package", PACKAGE)
        uid, grants = parse_permissions(output)
        _require(self.uid is None or uid == self.uid, "package UID changed")
        action.update(uid=uid, user_id=0, observed_grants=grants)
        return grants

    def _clock(self, action: dict[str, Any], deadline: float) -> int:
        # adb shell rejoins argv: a format containing spaces is not one remote argument.
        _, output = self._command(action, deadline, "date", "+%s%3N,%z")
        match = re.fullmatch(r"(\d{13}),([+-]\d{4})\s*", output)
        _require(match is not None, "unsupported device millisecond clock/UTC offset")
        assert match is not None
        _require(self.utc_offset in (None, match[2]), "device timezone changed")
        self.utc_offset = match[2]
        return int(match[1])

    def _history(self, action: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
        _, output = self._command(
            action, deadline, "dumpsys", "activity", "exit-info", PACKAGE
        )
        assert self.utc_offset is not None
        records = parse_exit_history(output, self.utc_offset)
        action["exit_records"] = records
        return records

    def initial(self) -> dict[str, bool]:
        """Require both initial grants, pin UID/user0, save full exit-history baseline."""
        _require(not self.started, "initial permission observation must be first")
        _require(
            not self.path.exists() and not self.path.is_symlink(),
            "refusing existing evidence",
        )
        self.started = True
        with self._action("initial") as (action, deadline):
            if self.use_root:
                _, identity = self._command(action, deadline, *ROOT_ID_ARGS)
                _require(
                    re.match(r"^uid=0(?:\(|\s|$)", identity.strip()) is not None,
                    "selected permission root identity is not uid 0",
                )
            _, sdk = self._command(action, deadline, "getprop", "ro.build.version.sdk")
            _require(
                sdk.strip().isdecimal() and int(sdk.strip()) >= 31,
                "requires Android API >=31",
            )
            self.document["api"] = int(sdk.strip())
            grants = self._permissions(action, deadline)
            _require(
                all(grants.values()), "initial SCAN and CONNECT must both be granted"
            )
            self.uid = action["uid"]
            self.document["uid"] = self.uid
            _, listing = self._command(
                action,
                deadline,
                "cmd",
                "package",
                "list",
                "packages",
                "-U",
                "--user",
                "0",
                PACKAGE,
            )
            verify_package_uid_listing(listing, self.uid)
            self._clock(action, deadline)
            records = self._history(action, deadline)
            self.baseline = {r["fingerprint"] for r in records}
            self.document["baseline_exit_records"] = records
            self.initial_grants = dict(grants)
            self.document["initial_grants"] = dict(grants)
        self.initialized = True
        return dict(grants)

    def _ready(self) -> None:
        _require(
            self.initialized and not self.finished,
            "initial permissions not established",
        )

    def _change(
        self, action: dict[str, Any], deadline: float, verb: str, permission: str
    ) -> None:
        self._command(
            action,
            deadline,
            *permission_mutation_arguments(verb, permission, use_root=self.use_root),
        )

    def deny_startup(self) -> dict[str, bool]:
        """Revoke both permissions once, then strictly read both user0 runtime grants."""
        self._ready()
        _require(
            self.old_identity is None
            and not any(a["label"] == "deny-startup" for a in self.document["actions"]),
            "startup deny cannot be repeated or follow connected revocation",
        )
        with self._action("deny-startup") as (action, deadline):
            self._permissions(action, deadline)
            for permission in PERMISSIONS:
                self._change(action, deadline, "revoke", permission)
            grants = self._permissions(action, deadline)
            _require(not any(grants.values()), "startup deny readback failed")
        return dict(grants)

    def grant(self, *, label: str = "grant") -> dict[str, bool]:
        """Grant both; launching/recovery of any new native instance is caller-owned."""
        self._ready()
        _require(
            self.old_identity is None or self.death_confirmed,
            "death gate unresolved; use finish for recovery",
        )
        _require(
            bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", label)), "invalid grant label"
        )
        with self._action("grant:" + label) as (action, deadline):
            return self._grant(action, deadline)

    def _grant(self, action: dict[str, Any], deadline: float) -> dict[str, bool]:
        # Recheck UID/user before mutation, including cleanup after a failed action.
        self._permissions(action, deadline)
        errors: list[str] = []
        for permission in PERMISSIONS:
            try:
                self._change(action, deadline, "grant", permission)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(f"{permission}: {error}")
        grants = self._permissions(action, deadline)
        _require(
            not errors and all(grants.values()), f"grant/readback failed: {errors}"
        )
        return dict(grants)

    def _identity(self, identity: AndroidProcessIdentity) -> AndroidProcessIdentity:
        _require(
            type(identity) is dict
            and set(identity) == set(AndroidProcessIdentity.__annotations__),
            "invalid old process identity fields",
        )
        _require(
            all(
                type(identity[k]) is int and identity[k] > 0
                for k in ("pid", "uid", "start_ticks", "native_started_unix_ms")
            )
            and type(identity["user_id"]) is int
            and identity["user_id"] == 0
            and identity["uid"] == self.uid
            and type(identity["process_name"]) is str
            and identity["process_name"] == PACKAGE,
            "old identity PID/UID/user/process mismatch",
        )
        return identity.copy()

    def _stat(self, action: dict[str, Any], deadline: float, pid: int) -> int | None:
        path = f"/proc/{pid}/stat"
        code, output = self._command(action, deadline, "cat", path, check=False)
        if code:
            _require(
                code == 1
                and output.strip() == f"cat: {path}: No such file or directory",
                "ambiguous /proc disappearance (access/transport failure is not death)",
            )
            return None
        return _start_ticks(output, pid)

    def revoke_connected(
        self, old_identity: AndroidProcessIdentity, timeout: float = 4.0
    ) -> dict[str, bool]:
        """Attempt CONNECT revoke exactly once, with a <=4s total remaining budget.

        Return actual SCAN/CONNECT grants: permission-group coupling may deny both.
        This is not a death verdict. No radio changes, kills, or relaunches occur.
        """
        self._ready()
        _deadline(timeout, 4.0)
        _require(self.old_identity is None, "connected revoke cannot be retried")
        identity = self._identity(old_identity)
        self.old_identity = identity
        with self._action("revoke-connected", timeout, maximum=4.0) as (
            action,
            deadline,
        ):
            action["old_identity"] = dict(identity)
            grants = self._permissions(action, deadline)
            _require(all(grants.values()), "connected revoke requires both grants")
            pid = identity["pid"]
            _require(
                self._stat(action, deadline, pid) == identity["start_ticks"],
                "old process instance changed",
            )
            _, status = self._command(action, deadline, "cat", f"/proc/{pid}/status")
            _require(
                re.findall(r"(?m)^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", status)
                == [(str(self.uid),) * 4],
                "old process real/effective UID mismatch",
            )
            _, cmdline = self._command(action, deadline, "cat", f"/proc/{pid}/cmdline")
            _require(cmdline.rstrip("\0") == PACKAGE, "old process cmdline mismatch")
            _require(
                self._stat(action, deadline, pid) == identity["start_ticks"],
                "old process instance changed during pin",
            )
            self.revoke_time_ms = self._clock(action, deadline)
            _require(
                identity["native_started_unix_ms"] <= self.revoke_time_ms,
                "native start timestamp after revoke",
            )
            self.revoke_monotonic = time.monotonic()
            action["revoke_unix_ms"] = self.revoke_time_ms
            self._change(action, deadline, "revoke", CONNECT)
            grants = self._permissions(action, deadline)
            _require(not grants[CONNECT], "CONNECT revoke survived permission readback")
        self.revoke_confirmed = True
        return dict(grants)

    def await_permission_death(
        self, identity: AndroidProcessIdentity, timeout: float = 6.0
    ) -> dict[str, Any]:
        """Require old instance gone AND one new exact reason-8 OS exit record.

        timeout <=6 bounds this call; the exit timestamp window additionally ends
        <=6s after revoke_connected's mutation boundary, not after a delayed call.
        pidof alone, reason10, missing/ambiguous/stale records, survival, inaccessible
        procfs and clock drift all fail closed. PID reuse is checked by start_ticks.
        """
        self._ready()
        checked = self._identity(identity)
        _require(
            checked == self.old_identity
            and self.revoke_confirmed
            and not self.death_confirmed,
            "death requires matching confirmed connected revoke",
        )
        assert self.revoke_monotonic is not None and self.revoke_time_ms is not None
        with self._action("await-permission-death", timeout, maximum=6.0) as (
            action,
            deadline,
        ):
            deadline = min(deadline, self.revoke_monotonic + 6.0)
            action["deadline_monotonic"] = deadline
            while True:
                _require(
                    time.monotonic() < deadline,
                    "permission death deadline expired; ROM survival is unsupported",
                )
                ticks = self._stat(action, deadline, checked["pid"])
                records = self._history(action, deadline)
                now_ms = self._clock(action, deadline)
                elapsed = time.monotonic() - self.revoke_monotonic
                _require(
                    0 <= now_ms - self.revoke_time_ms <= 6000
                    and abs((now_ms - self.revoke_time_ms) / 1000 - elapsed) <= 1.0,
                    "device clock outside bounded revoke window",
                )
                fresh = [
                    r
                    for r in records
                    if r["pid"] == checked["pid"]
                    and r["fingerprint"] not in self.baseline
                ]
                _require(len(fresh) <= 1, "ambiguous new old-PID exit records")
                if fresh:
                    record = fresh[0]
                    _require(
                        record["package_uid"] == record["real_uid"] == checked["uid"]
                        and record["user_id"] == 0
                        and record["process_name"] == PACKAGE,
                        "exit PID/packageUid/realUid/user/process mismatch",
                    )
                    _require(
                        checked["native_started_unix_ms"]
                        <= self.revoke_time_ms
                        <= record["timestamp_unix_ms"]
                        <= now_ms,
                        "stale/out-of-window exit record",
                    )
                    _require(
                        record["reason"] == 8
                        and record["reason_name"] == "PERMISSION CHANGE",
                        "OS exit is not reason8 PERMISSION CHANGE (reason10 is not permission death)",
                    )
                    if ticks != checked["start_ticks"]:
                        action.update(
                            old_instance_gone=True,
                            observed_start_ticks=ticks,
                            exit_record=record,
                        )
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "old process survived or new permission exit evidence missing"
                    )
                time.sleep(min(POLL_SECONDS, remaining))
        self.death_confirmed = True
        return dict(record)

    def finish(self, primary: BaseException | None) -> list[str]:
        """Restore initial grants/readback; return cleanup errors, never mask primary.

        Does not release the caller's lease. A failed death gate cannot become a
        pass through successful restoration. Call before host-owner process recovery.
        """
        if self.finished:
            return ["permission controller was already finished"]
        if not self.started:
            return ["initial permission observation never started; evidence untouched"]
        self.document["primary_error"] = (
            None
            if primary is None
            else {"type": type(primary).__name__, "message": str(primary)}
        )
        errors: list[str] = []
        if (
            primary is None
            and self.old_identity is not None
            and not self.death_confirmed
        ):
            errors.append(
                "permission death gate incomplete; restoration is not death evidence"
            )
        try:
            if self.initial_grants is not None:
                with self._action("restore-initial-grants") as (action, deadline):
                    self.document["final_grants"] = self._grant(action, deadline)
            elif primary is None:
                errors.append("initial permission grants not established")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"restore permissions: {error}")
        self.finished = True
        self.document["cleanup_errors"] = errors
        failed = any(a["status"] != "confirmed" for a in self.document["actions"])
        self.document["status"] = (
            "failed" if primary is not None or errors or failed else "passed"
        )
        try:
            self._publish()
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(f"publish permission cleanup: {error}")
            self.document["status"] = "failed"
        if primary is not None:
            for error in errors:
                primary.add_note(error)
        return errors

    finalize = finish
