"""Non-RF Android lifecycle/diagnostic-control sealing and scope-preserving replay.

This is host-submitted integrity/consistency evidence, not session authority,
installed-binary/signing attestation, a complete ELF/package gate or an RF gate.
The runner owns all live checks and phone release/quarantine. Publication cannot
retroactively change that outcome. Install only the successfully frozen APK.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from . import evidence_files
from .bleng_android_close_diagnostics import LIVE_LOGCAT_TAGS
from .capture_io import publish_new_json

SCOPE = "formal-android-managed-lifecycle"
IDLE_CONTROL_SCOPE = "formal-android-managed-idle-control"
START_HOLD_CONTROL_SCOPE = "formal-android-managed-start-hold-control"
MAX_APK_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_APK_ENTRIES = 20_000
MAX_APK_EXPANDED_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
# Each operation is finite even if the caller supplies an excessively remote deadline.
MAX_SECONDS = 120
DIRECTORIES = {"candidate", "helpers", "platform", "automation"}
ROOT_FILES = {
    "candidate.apk",
    "phone.json",
    "prepared-result.json",
    "result.json",
    "manifest.json",
}
FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "device",
    "app_id",
    "apk_sha256",
    "scenario_status",
    "session_status",
    "host_gate_status",
    "status",
    "observations",
    "startup_snapshot",
    "primary_error",
    "cleanup_errors",
    "phone_cleanup_errors",
    "phone_status",
    "cleanup_order",
    "rf",
}


class LifecycleEvidenceError(ValueError):
    """The recorded evidence cannot support this scoped integrity claim."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleEvidenceError(message)


def _check_deadline(deadline: float) -> None:
    _require(
        type(deadline) in (int, float) and math.isfinite(deadline),
        "finite absolute deadline required",
    )
    if time.monotonic() >= deadline:
        raise TimeoutError("lifecycle evidence deadline exceeded")


def _path(path: Path) -> Path:
    _require(".." not in path.parts, "path traversal")
    path = path.absolute()
    evidence_files.no_links(path, require=_require)
    return path


def _regular(info: os.stat_result, limit: int) -> None:
    _require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "regular non-hardlinked file required",
    )
    _require(0 <= info.st_size <= limit, "file size exceeds limit")


def _sig(info: os.stat_result) -> tuple[int, ...]:
    return (*evidence_files.signature(info), info.st_nlink)


def _open(path: Path, limit: int) -> BinaryIO:
    _path(path)
    before = path.lstat()
    _regular(before, limit)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    if _sig(before) != _sig(os.fstat(stream.fileno())):
        stream.close()
        raise LifecycleEvidenceError("file changed at open")
    return stream


def _read(
    path: Path, *, limit: int, deadline: float, output: BinaryIO | None = None
) -> tuple[str, int, bytes]:
    """Deadline-aware stable-descriptor read; only small JSON reads retain bytes."""
    digest, size, blocks = hashlib.sha256(), 0, []
    with _open(path, limit) as stream:
        before = os.fstat(stream.fileno())
        while True:
            _check_deadline(deadline)
            block = stream.read(min(CHUNK_BYTES, limit - size + 1))
            if not block:
                break
            size += len(block)
            _require(size <= limit, "stream exceeds byte limit")
            digest.update(block)
            if output is not None:
                output.write(block)
            elif path.suffix == ".json":
                blocks.append(block)
        _require(
            _sig(before) == _sig(os.fstat(stream.fileno())) == _sig(path.lstat())
            and size == before.st_size,
            "file drift during read/copy",
        )
    _check_deadline(deadline)
    return digest.hexdigest(), size, b"".join(blocks)


def _strict_json(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def number(text):
        value = float(text)
        _require(math.isfinite(value), "invalid JSON number")
        return value

    def invalid(text):
        raise LifecycleEvidenceError(f"invalid JSON number: {text}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_float=number,
            parse_constant=invalid,
        )
    except (UnicodeError, RecursionError) as error:
        raise LifecycleEvidenceError("invalid/deep JSON") from error
    _require(type(value) is dict, "JSON object required")
    return value


def _json(path: Path, deadline: float) -> dict:
    return _strict_json(_read(path, limit=MAX_JSON_BYTES, deadline=deadline)[2])


def _apk(path: Path, deadline: float) -> None:
    """Release-shaped inventory only: no ELF, signing or installed identity claim."""
    with _open(path, MAX_APK_BYTES) as raw:
        before = _sig(os.fstat(raw.fileno()))
        count = evidence_files.zip_directory_bound(
            raw, max_entries=MAX_APK_ENTRIES, require=_require
        )
        files: dict[str, int] = {}
        names: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(raw) as archive:
                entries = archive.infolist()
                _require(len(entries) == count, "bad APK directory count")
                for entry in entries:
                    _check_deadline(deadline)
                    name = evidence_files.safe_name(
                        entry.filename[:-1] if entry.is_dir() else entry.filename,
                        require=_require,
                    )
                    _require(
                        entry.orig_filename == entry.filename and name not in names,
                        "duplicate/altered APK path",
                    )
                    names.add(name)
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    _require(
                        kind in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                        and not entry.flag_bits & 1,
                        "linked/special/encrypted APK entry",
                    )
                    total += entry.file_size
                    _require(
                        0 <= entry.file_size <= MAX_FILE_BYTES
                        and total <= MAX_APK_EXPANDED_BYTES,
                        "APK expanded size exceeds limit",
                    )
                    size = 0
                    with archive.open(entry) as stream:
                        while block := stream.read(CHUNK_BYTES):
                            _check_deadline(deadline)
                            size += len(block)
                            _require(
                                size <= entry.file_size and size <= MAX_FILE_BYTES,
                                "APK entry exceeds size limit",
                            )
                    _require(
                        size == entry.file_size and (not entry.is_dir() or size == 0),
                        "bad APK entry size",
                    )
                    if not entry.is_dir():
                        files[name] = size
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            raise LifecycleEvidenceError(f"invalid APK: {error}") from error
        _require(
            before == _sig(os.fstat(raw.fileno())) == _sig(path.lstat()),
            "APK drift during inspection",
        )
    _require(
        files.get("assets/index.android.bundle", 0) > 0, "missing/empty bundled JS"
    )
    _require(
        any(
            size > 0 and re.fullmatch(r"classes(?:[1-9][0-9]*)?\.dex", name)
            for name, size in files.items()
        ),
        "missing/empty dex",
    )
    libraries = {name for name in files if re.fullmatch(r"lib/[^/]+/[^/]+\.so", name)}
    abis = {name.split("/")[1] for name in libraries}
    _require(bool(abis), "missing BLeng native images")
    required = {"libbleng_uniffi.so", "libBleHubReactNativeBleng.so"}
    for abi in abis:
        _require(
            all(files.get(f"lib/{abi}/{name}", 0) > 0 for name in required),
            f"missing/empty formal BLeng native image for {abi}",
        )
    _require(
        not any(
            ("blehub" in Path(name).name.lower() or "bleng" in Path(name).name.lower())
            and Path(name).name not in required
            for name in libraries
        ),
        "unexpected/legacy BleHub or BLeng native image",
    )
    _check_deadline(deadline)


def freeze_lifecycle_apk(
    source: Path, destination: Path, *, expected_sha256: str, deadline: float
) -> None:
    """Exclusively freeze stable bytes/hash before any device admission.

    The caller supplies an absolute monotonic deadline. On failure retract only
    this function's destination inode, never an old or substituted evidence file.
    """
    _check_deadline(deadline)
    deadline = min(deadline, time.monotonic() + MAX_SECONDS)
    _require(
        type(expected_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
        "canonical APK SHA256 required",
    )
    source, destination = _path(source), _path(destination)
    _require(
        source.suffix == ".apk" and destination.name == "candidate.apk",
        "source APK and fixed candidate.apk destination required",
    )

    # xb is the exclusive boundary (including a pre-existing ordinary file).
    owned = None
    try:
        source_signature = _sig(source.lstat())
        _regular(source.lstat(), MAX_APK_BYTES)
        _require(source.stat().st_size > 0, "empty APK")
        with destination.open("xb") as output:
            owned = _sig(os.fstat(output.fileno()))[:2]
            digest, size, _ = _read(
                source, limit=MAX_APK_BYTES, deadline=deadline, output=output
            )
        _require(digest == expected_sha256 and size > 0, "APK expected hash mismatch")
        _apk(destination, deadline)
        _require(
            _read(destination, limit=MAX_APK_BYTES, deadline=deadline)[:2]
            == (digest, size),
            "frozen APK drift",
        )
        _require(
            _read(source, limit=MAX_APK_BYTES, deadline=deadline)[:2] == (digest, size)
            and _sig(source.lstat()) == source_signature,
            "source APK drift after copy",
        )
        _require(_sig(destination.lstat())[:2] == owned, "frozen APK inode replaced")
    except BaseException as error:
        if owned is not None:
            try:
                if _sig(destination.lstat())[:2] == owned:
                    destination.unlink()
            except OSError as cleanup:
                error.add_note(f"frozen APK retraction: {cleanup}")
        raise


def _snapshot(attempt: Path, deadline: float) -> tuple[list[dict], list[dict], dict]:
    """Inventory the entire bounded tree, including empty directories and modes."""
    _path(attempt)
    root = attempt.lstat()
    _require(stat.S_ISDIR(root.st_mode), "attempt directory required")
    files, directories, signatures = (
        [],
        [],
        {"": (root.st_dev, root.st_ino, root.st_mode)},
    )
    aliases: set[str] = set()
    count = total = 0

    def visit(directory: Path) -> None:
        nonlocal count, total
        with os.scandir(directory) as entries:
            for entry in entries:
                _check_deadline(deadline)
                count += 1
                _require(count <= MAX_ENTRIES, "too many evidence entries")
                path = Path(entry.path)
                name = evidence_files.safe_name(
                    path.relative_to(attempt).as_posix(), require=_require
                )
                _require(name.casefold() not in aliases, "aliased evidence path")
                aliases.add(name.casefold())
                info = entry.stat(follow_symlinks=False)
                top = name.split("/")[0]
                _require(
                    top in DIRECTORIES or ("/" not in name and name in ROOT_FILES),
                    "unexpected evidence layout",
                )
                if stat.S_ISDIR(info.st_mode):
                    _require(top in DIRECTORIES, "unexpected evidence directory")
                    directories.append(
                        {"path": name, "mode": stat.S_IMODE(info.st_mode)}
                    )
                    signatures[name] = _sig(info)
                    visit(path)
                    _require(
                        _sig(info) == _sig(path.lstat()),
                        "directory drift during inventory",
                    )
                else:
                    _regular(info, MAX_FILE_BYTES)
                    _require(
                        top not in DIRECTORIES or "/" in name,
                        "owner evidence must be a directory",
                    )
                    total += info.st_size
                    _require(
                        total <= MAX_TOTAL_BYTES, "evidence total bytes exceed limit"
                    )
                    if name == "manifest.json":
                        continue
                    limit = (
                        min(MAX_FILE_BYTES, MAX_JSON_BYTES)
                        if path.suffix == ".json"
                        else MAX_FILE_BYTES
                    )
                    digest, size, raw = _read(path, limit=limit, deadline=deadline)
                    _require(
                        _sig(info) == _sig(path.lstat()), "file drift during inventory"
                    )
                    if path.suffix == ".json":
                        _strict_json(raw)
                    files.append(
                        {
                            "path": name,
                            "size": size,
                            "sha256": digest,
                            "mode": stat.S_IMODE(info.st_mode),
                        }
                    )
                    signatures[name] = _sig(info)

    visit(attempt)
    _require(
        (root.st_dev, root.st_ino, root.st_mode)
        == (attempt.stat().st_dev, attempt.stat().st_ino, attempt.stat().st_mode),
        "attempt directory replaced",
    )
    return (
        sorted(files, key=lambda row: row["path"]),
        sorted(directories, key=lambda row: row["path"]),
        signatures,
    )


def _strings(value: Any) -> bool:
    return type(value) is list and all(
        type(item) is str and bool(item) for item in value
    )


def _result(result: dict, *, prepared: bool = False) -> None:
    _require(
        type(result) is dict and set(result) == FIELDS,
        "invalid lifecycle result fields",
    )
    _require(
        type(result["schema_version"]) is int
        and result["schema_version"] == 1
        and result["scope"] in (SCOPE, IDLE_CONTROL_SCOPE, START_HOLD_CONTROL_SCOPE)
        and result["platform"] == "android"
        and result["rf"] == "not_run",
        "invalid lifecycle schema/scope/platform/RF claim",
    )
    run = result["run_id"]
    try:
        canonical = type(run) is str and str(uuid.UUID(run)) == run
    except (ValueError, AttributeError):
        canonical = False
    _require(canonical, "canonical run UUID required")
    _require(
        type(result["device"]) is str
        and re.fullmatch(r"[A-Za-z0-9_.:-]+", result["device"]) is not None
        and not result["device"].lower().startswith("emulator-"),
        "explicit Android device required",
    )
    _require(
        type(result["app_id"]) is str
        and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", result["app_id"]
        )
        is not None,
        "canonical app_id required",
    )
    _require(
        type(result["apk_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", result["apk_sha256"]) is not None,
        "canonical APK hash required",
    )
    for field in ("scenario_status", "session_status"):
        _require(result[field] in ("not_run", "failed", "passed"), f"invalid {field}")
    _require(
        result["host_gate_status"] in ("pending", "failed")
        and result["status"] in ("passed", "failed")
        and result["phone_status"]
        in ("not_acquired", "failed", "quarantined", "passed"),
        "invalid gate/status/phone status",
    )
    _require(
        result["primary_error"] is None
        or (type(result["primary_error"]) is str and bool(result["primary_error"])),
        "invalid primary error",
    )
    for field in (
        "observations",
        "cleanup_errors",
        "phone_cleanup_errors",
        "cleanup_order",
    ):
        _require(_strings(result[field]), f"invalid {field}")
    if "start-hold-control.record" in result["cleanup_order"]:
        _require(
            result["scope"] == START_HOLD_CONTROL_SCOPE,
            "start-hold cleanup cannot claim another scenario scope",
        )
    _require(type(result["startup_snapshot"]) is dict, "snapshot object required")
    if result["scope"] == IDLE_CONTROL_SCOPE:
        _require(
            result["startup_snapshot"] == {}
            and result["observations"] in ([], ["idle"]),
            "idle control cannot claim startup/close progress or a startup snapshot",
        )
    if result["scope"] == START_HOLD_CONTROL_SCOPE:
        trace = ["idle", "started", "closed"]
        _require(
            result["observations"] == trace[: len(result["observations"])]
            and (
                result["scenario_status"] != "passed" or result["observations"] == trace
            ),
            "invalid start-hold observation trace",
        )
    if prepared:
        _require(
            result["host_gate_status"] == "pending",
            "prepared host gate must remain pending",
        )
        return
    if result["status"] == "passed":
        _require(
            result["scenario_status"]
            == result["session_status"]
            == result["phone_status"]
            == "passed"
            and result["host_gate_status"] == "pending"
            and result["primary_error"] is None
            and not result["cleanup_errors"]
            and not result["phone_cleanup_errors"],
            "inconsistent lifecycle success",
        )
        _require(
            result["observations"]
            == (
                ["idle"]
                if result["scope"] == IDLE_CONTROL_SCOPE
                else ["idle", "started", "closed"]
            ),
            "incomplete lifecycle observation trace",
        )
        snapshot = result["startup_snapshot"]
        auth = {"notRequired", "notDetermined", "denied", "restricted", "authorized"}
        _require(
            result["scope"] == IDLE_CONTROL_SCOPE
            or (
                set(snapshot)
                == {"state", "scan_authorization", "connection_authorization"}
                and snapshot["state"]
                in {"unknown", "unavailable", "poweredOff", "poweredOn"}
                and snapshot["scan_authorization"] in auth
                and snapshot["connection_authorization"] in auth
            ),
            "invalid startup snapshot (not a readiness gate)",
        )
        order = result["cleanup_order"]
        labels = [
            "automation.close",
            "candidate.finalize",
            "bundle.prepare",
            "phone.finish",
        ]
        _require(
            all(order.count(label) == 1 for label in labels)
            and [order.index(label) for label in labels]
            == sorted(order.index(label) for label in labels),
            "invalid cleanup order",
        )
    else:
        _require(
            result["host_gate_status"] == "failed"
            and (
                result["primary_error"] is not None
                or result["cleanup_errors"]
                or result["phone_cleanup_errors"]
            ),
            "failed lifecycle must preserve a cause and failed host gate",
        )


def _adb_transport_observation(doc: dict, server: dict, result: dict) -> None:
    """Replay bounded diagnostic meaning, never transport ownership or a verdict."""
    _require(
        set(doc)
        == {
            "run_id",
            "device",
            "host_session_id",
            "device_session_id",
            "server_identity",
            "system_port",
            "endpoint",
            "started_monotonic",
            "finished_monotonic",
            "budget_seconds",
            "expected_boot_id",
            "boot_id_matches_expected",
            "stages",
            "error",
            "limitations",
        },
        "invalid ADB transport receipt fields",
    )

    def finite(value) -> bool:
        try:
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            return False

    def error(value) -> None:
        _require(
            value is None
            or (
                type(value) is dict
                and {"type", "message"} <= value.keys() <= {"type", "message", "notes"}
                and type(value["type"]) is str
                and bool(value["type"])
                and type(value["message"]) is str
                and ("notes" not in value or _strings(value["notes"]))
            ),
            "invalid ADB transport error",
        )

    start, end, budget = (
        doc[key]
        for key in ("started_monotonic", "finished_monotonic", "budget_seconds")
    )
    _require(
        finite(start)
        and finite(end)
        and start <= end
        and finite(budget)
        and 0 < budget <= 3,
        "invalid ADB transport time/budget",
    )
    boot = server.get("boot_id")
    uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    _require(
        type(boot) is str
        and re.fullmatch(uuid_pattern, boot) is not None
        and doc["expected_boot_id"] == boot
        and _strings(doc["limitations"])
        and bool(doc["limitations"]),
        "ADB transport expected boot identity/limitations conflict",
    )
    error(doc["error"])
    stages = doc["stages"]
    names = [
        "server_version",
        "cached_state",
        "transport_select",
        "service_accept",
        "boot_id",
    ]
    _require(
        type(stages) is list and len(stages) <= len(names),
        "invalid ADB transport stages",
    )
    previous = start
    failed = False
    comparison = None
    for index, stage in enumerate(stages):
        _require(
            not failed
            and type(stage) is dict
            and set(stage)
            == {
                "name",
                "started_monotonic",
                "finished_monotonic",
                "output",
                "value",
                "error",
            }
            and stage["name"] == names[index],
            "ADB transport stages must be a stopped sequence prefix",
        )
        error(stage["error"])
        failed = stage["error"] is not None
        began, finished = stage["started_monotonic"], stage["finished_monotonic"]
        # Success must fit the I/O budget; a failed socket timeout may return late.
        # Receipt end also includes the independent postguard.
        _require(
            finite(began)
            and finite(finished)
            and previous <= began <= finished <= end
            and (failed or finished - start <= budget),
            "ADB transport stage time/order exceeds global I/O budget",
        )
        previous = finished
        output, value = stage["output"], stage["value"]
        # ascii/backslashreplace expands a non-ASCII byte to four characters;
        # framed FAIL/payload reads have 8 header bytes + <=64 payload bytes.
        maximum = 516 if index == 4 else 264
        _require(
            type(output) is str
            and output.isascii()
            and len(output) <= maximum
            and (
                value is None
                or (type(value) is str and value.isascii() and len(value) <= maximum)
            ),
            "ADB transport output/value exceeds stage byte budget",
        )
        _require(
            began - start < budget or (failed and output == "" and value is None),
            "ADB transport stage started after budget with I/O output/value",
        )
        if index == 4 and value is not None:
            _require(
                re.fullmatch(uuid_pattern, value) is not None
                and output == value + "\n",
                "ADB transport boot value/output conflict",
            )
            comparison = value == boot
        if not failed:
            if index == 0:
                valid = (
                    type(value) is str
                    and re.fullmatch(r"[0-9a-fA-F]{4}", value) is not None
                    and output == "OKAY0004" + value
                )
            elif index == 1:
                valid = value == "device" and output == "OKAY0006device"
            elif index in (2, 3):
                valid = value == output == "OKAY"
            else:
                valid = value == boot and comparison is True
            _require(valid, "ADB transport error-free stage value/output conflict")
    matched = doc["boot_id_matches_expected"]
    _require(
        (matched is None and comparison is None)
        or (type(matched) is bool and matched is comparison),
        "ADB transport boot_id_matches_expected conflict",
    )
    _require(
        doc["error"] is not None or (len(stages) == len(names) and not failed),
        "ADB transport incomplete/failed stages lost their error",
    )
    _require(
        doc["error"] is None or result["status"] == "failed",
        "failed ADB transport observation cannot support a passed bundle",
    )


def _facts(attempt: Path, result: dict, files: list[dict], deadline: float) -> None:
    _result(result)
    by_name = {row["path"]: row for row in files}
    documents = {
        name: _json(attempt / name, deadline)
        for name in by_name
        if name.endswith(".json") and name != "result.json"
    }
    for name, doc in documents.items():
        for field, expected in (
            ("run_id", result["run_id"]),
            ("device", result["device"]),
            ("serial", result["device"]),
            ("app_id", result["app_id"]),
            ("apk_sha256", result["apk_sha256"]),
        ):
            if field in doc:
                _require(doc[field] == expected, f"conflicting {field} in {name}")
    # Start-hold and lifecycle share state names, not scenario semantics. Keep
    # the independently sealed runner record bound even on failed attempts.
    control = documents.get("automation/start-hold-control.json")
    if (
        result["scope"] == START_HOLD_CONTROL_SCOPE
        and result["scenario_status"] == "passed"
    ):
        _require(
            control is not None
            or (
                result["status"] == "failed"
                and any(
                    error.startswith("start-hold-control.record:")
                    for error in result["cleanup_errors"]
                )
            ),
            "missing start-hold scenario record",
        )
    if control is not None:
        _require(
            set(control)
            == {
                "schema_version",
                "run_id",
                "scope",
                "browse",
                "quiet_seconds",
                "hold_observation",
            }
            and type(control["schema_version"]) is int
            and control["schema_version"] == 1
            and control["run_id"] == result["run_id"]
            and control["scope"] == result["scope"] == START_HOLD_CONTROL_SCOPE
            and control["browse"] is False
            and type(control["quiet_seconds"]) is int
            and control["quiet_seconds"] == 5,
            "conflicting start-hold scenario record",
        )
        window = control["hold_observation"]
        _require(
            type(window) is dict
            and set(window)
            in (
                set(),
                {"started_monotonic"},
                {"started_monotonic", "finished_monotonic"},
            )
            and all(
                type(value) in (int, float) and math.isfinite(value) and value >= 0
                for value in window.values()
            ),
            "invalid start-hold quiet window",
        )
        if "finished_monotonic" in window:
            _require(
                5 <= window["finished_monotonic"] - window["started_monotonic"] <= 300,
                "incomplete or unbounded start-hold quiet window",
            )
        if result["scenario_status"] == "passed":
            _require(
                "finished_monotonic" in window,
                "missing completed start-hold quiet window",
            )
    if "candidate.apk" in by_name:
        _require(
            by_name["candidate.apk"]["sha256"] == result["apk_sha256"],
            "candidate APK hash mismatch",
        )
        _apk(attempt / "candidate.apk", deadline)
    prepared = documents.get("prepared-result.json")
    if prepared is not None:
        _result(prepared, prepared=True)
        for field in FIELDS - {
            "session_status",
            "host_gate_status",
            "status",
            "phone_status",
            "phone_cleanup_errors",
            "cleanup_order",
            "cleanup_errors",
        }:
            _require(
                prepared[field] == result[field], f"prepared-result drift: {field}"
            )
        for field in ("cleanup_order", "cleanup_errors", "phone_cleanup_errors"):
            _require(
                result[field][: len(prepared[field])] == prepared[field],
                f"prepared errors/order lost: {field}",
            )
    phone = documents.get("phone.json")
    if phone is not None:
        _require(
            phone.get("run_id") == result["run_id"]
            and phone.get("status") == result["phone_status"]
            and phone.get("actions") == []
            and phone.get("initial_state") is None
            and phone.get("final_state") is None,
            "phone journal conflict or radio actions",
        )
        _require(
            phone.get("cleanup_errors") == result["phone_cleanup_errors"],
            "phone cleanup errors lost",
        )
    else:
        _require(
            result["phone_status"] == "not_acquired", "missing acquired phone journal"
        )
    # Even failed bundles must not hide a known session identity conflict.
    start, close, bound = (
        documents.get(name)
        for name in (
            "automation/start.json",
            "automation/close.json",
            "platform/bound-session.json",
        )
    )
    known = [
        doc[key]
        for doc, key in (
            (start, "session_id"),
            (close, "session_id"),
            (bound, "host_session_id"),
        )
        if doc is not None and doc.get(key) is not None
    ]
    _require(
        not known
        or all(
            type(value) is str and bool(value) and value == known[0] for value in known
        ),
        "conflicting automation host session IDs",
    )
    final = documents.get("candidate/finalization.json")
    identity = documents.get("candidate/bound-identity.json")
    if final is not None and final.get("identity") is not None:
        app = final["identity"]
        _require(
            type(app) is dict
            and app.get("serial") == result["device"]
            and app.get("package") == result["app_id"]
            and app.get("apk_sha256") == result["apk_sha256"],
            "candidate identity conflict",
        )
        _require(
            identity is None or app == identity,
            "bound/final candidate identity conflict",
        )
    for probe in ("server-status", "adb-transport"):
        status = documents.get(f"platform/{probe}.json")
        if status is None:
            continue
        prepared_owner = documents.get("platform/prepared-owner.json")
        server = bound.get("server_identity") if bound is not None else None
        observed_server = status.get("server_identity")
        port = prepared_owner.get("system_port") if prepared_owner is not None else None
        _require(
            bound is not None
            and bound.get("run_id") == result["run_id"]
            and prepared_owner is not None
            and prepared_owner.get("run_id") == result["run_id"]
            and status.get("run_id") == result["run_id"]
            and all(
                type(bound.get(field)) is str
                and bool(bound[field])
                and status.get(field) == bound[field]
                for field in ("host_session_id", "device_session_id")
            ),
            "server status requires the original bound/prepared session selectors",
        )
        _require(
            type(server) is dict
            and server.get("serial") == result["device"]
            and server.get("package") == "io.appium.uiautomator2.server"
            and type(server.get("pid")) is int
            and 0 < server["pid"] <= 2147483647
            and type(observed_server) is dict
            and observed_server.keys() == server.keys()
            and all(
                type(observed_server[key]) is type(value)
                and observed_server[key] == value
                for key, value in server.items()
            ),
            "server status original server identity conflict",
        )
        _require(
            type(port) is int
            and 1 <= port <= 65535
            and type(status.get("system_port")) is int
            and status["system_port"] == port
            and status.get("endpoint")
            == (
                "127.0.0.1:5037"
                if probe == "adb-transport"
                else f"http://127.0.0.1:{port}/status"
            ),
            "server status original forwarding port/endpoint conflict",
        )
        if probe == "adb-transport":
            _require(
                status.get("device") == result["device"]
                and prepared_owner.get("app_id") == result["app_id"]
                and prepared_owner.get("status") == "prepared",
                "ADB transport original device/prepared App conflict",
            )
            _adb_transport_observation(status, server, result)
        # Response/timeout/ready are observations, never DELETE or cleanup proof.
    # Optional server logs remain observations, including failed reads. Validate
    # their selector before the failed-bundle return, never their log contents.
    for name, doc in documents.items():
        argv = doc.get("argv")
        if (
            re.fullmatch(r"platform/[0-9]{3,}-read\.json", name) is None
            or type(argv) is not list
            or "logcat" not in argv
        ):
            continue
        server = bound.get("server_identity") if bound is not None else None
        _require(
            bound is not None
            and bound.get("run_id") == result["run_id"]
            and type(server) is dict
            and server.get("serial") == result["device"]
            and server.get("package") == "io.appium.uiautomator2.server"
            and type(server.get("pid")) is int
            and 0 < server["pid"] <= 2147483647,
            "server log capture requires the same-run bound server identity",
        )
        _require(
            len(argv) == 13
            and all(type(arg) is str for arg in argv)
            and bool(argv[0])
            and argv[1:]
            == [
                "-s",
                result["device"],
                "logcat",
                "-d",
                "-v",
                "threadtime",
                "--pid",
                str(server["pid"]),
                "-t",
                "1000",
                "appium:I",
                "*:S",
            ],
            "server log original serial/PID/tag selector conflict",
        )
    diagnostic_prefix = "platform/close-diagnostics/"
    diagnostics = {
        name.removeprefix(diagnostic_prefix): doc
        for name, doc in documents.items()
        if name.startswith(diagnostic_prefix)
    }
    for name in ("before-close.json", "before-close-failed.json"):
        doc = diagnostics.get(name)
        if doc is None:
            continue
        complete = name == "before-close.json"
        bound_identity = by_name.get("candidate/bound-identity.json")
        for field, expected in (
            (
                "candidate_identity_sha256",
                bound_identity["sha256"] if bound_identity else None,
            ),
            ("serial", identity.get("serial") if identity else None),
            ("pid", identity.get("pid") if identity else None),
        ):
            if complete or field in doc:
                _require(
                    expected is not None
                    and doc.get(field) == expected
                    and (field != "pid" or type(doc[field]) is int),
                    f"before-Close {field} identity conflict",
                )
        if complete:
            _require(
                doc.get("run_id") == result["run_id"]
                and type(doc.get("elementId")) is str
                and bool(doc["elementId"].strip()),
                "before-Close same-element identity required",
            )
            for field in ("before_rect", "after_rect", "window_rect"):
                rect = doc.get(field)
                _require(
                    type(rect) is dict
                    and all(
                        type(rect.get(key)) in (int, float)
                        and math.isfinite(rect[key])
                        and (key not in {"width", "height"} or rect[key] > 0)
                        for key in ("x", "y", "width", "height")
                    ),
                    f"invalid before-Close {field}",
                )
            _require(
                doc["before_rect"] == doc["after_rect"],
                "before-Close element rect changed",
            )
        rawfiles = doc.get("rawfiles", {})
        _require(
            type(rawfiles) is dict
            and set(rawfiles) <= {"xml", "png"}
            and (not complete or set(rawfiles) == {"xml", "png"}),
            "missing/invalid before-Close raw references",
        )
        for kind, ref in rawfiles.items():
            relative = diagnostic_prefix + f"before-close.{kind}"
            row = by_name.get(relative)
            _require(type(ref) is dict, "invalid before-Close raw reference")
            path = ref.get("path")
            limit = (1 if kind == "xml" else 8) * 1024 * 1024
            _require(
                type(path) is str
                and Path(path).is_absolute()
                and ".." not in Path(path).parts
                and Path(path).parts[-3:] == tuple(relative.split("/"))
                and row is not None
                and ref.get("sha256") == row["sha256"]
                and type(ref.get("size")) is int
                and ref["size"] == row["size"]
                and row["size"] <= limit,
                "before-Close raw path/hash/size conflict or limit exceeded",
            )
            if kind == "png":
                # The absolute path records provenance, not an offline read target.
                with _open(attempt / relative, limit) as stream:
                    _require(
                        stream.read(8) == b"\x89PNG\r\n\x1a\n",
                        "invalid before-Close PNG signature",
                    )
    captures = []
    for name, doc in diagnostics.items():
        argv = doc.get("argv")
        if argv is not None:
            _require(
                type(argv) is list
                and len(argv) >= 4
                and all(type(arg) is str for arg in argv)
                and argv[1:3] == ["-s", result["device"]],
                "Close diagnostics command device conflict",
            )
        if "candidate_identity" not in doc and (argv is None or argv[3] != "logcat"):
            continue
        selector = doc.get("candidate_identity")
        _require(type(selector) is dict, "missing Close diagnostics candidate selector")
        pin, path = selector.get("pin"), selector.get("path")
        _require(
            identity is not None
            and type(path) is str
            and Path(path).is_absolute()
            and ".." not in Path(path).parts
            and Path(path).parts[-2:] == ("candidate", "bound-identity.json")
            and type(pin) is list
            and len(pin) == 3
            and all(type(value) is int and value >= 0 for value in pin[:2])
            and pin[2] == by_name["candidate/bound-identity.json"]["sha256"],
            "Close diagnostics candidate identity hash/path conflict",
        )
        if name in ("live-intent.json", "live-stop.json"):
            _require(
                argv is not None
                and argv[3:]
                == [
                    "logcat",
                    "-b",
                    "main,system,crash,events",
                    "-v",
                    "threadtime",
                    "-T",
                    "1",
                    *LIVE_LOGCAT_TAGS,
                ],
                "live diagnostics buffer/tag selector conflict",
            )
            start, end = doc.get("started_monotonic"), doc.get("deadline_monotonic")
            _require(
                all(
                    type(value) in (int, float) and math.isfinite(value)
                    for value in (start, end)
                )
                and 0 <= start < end <= start + 300
                and doc.get("maximum_raw_bytes") == 8 * 1024 * 1024,
                "invalid live diagnostics bounds",
            )
            bound_name = diagnostic_prefix + "live-bound-identity.json"
            _require(
                bound_name in by_name and by_name[bound_name]["sha256"] == pin[2],
                "live diagnostics original identity copy conflict",
            )
            if name == "live-stop.json":
                raw_name = diagnostic_prefix + "live-logcat.raw"
                raw = doc.get("raw")
                _require(
                    type(raw) is dict
                    and raw_name in by_name
                    and raw.get("sha256") == by_name[raw_name]["sha256"]
                    and raw.get("size") == by_name[raw_name]["size"]
                    and 0 <= raw["size"] <= 8 * 1024 * 1024,
                    "live diagnostics raw reference conflict",
                )
                arrivals = doc.get("arrivals")
                _require(
                    type(arrivals) is list and len(arrivals) <= 64,
                    "invalid live diagnostics arrival window",
                )
                offset, observed = 0, start
                for arrival in arrivals:
                    _require(
                        type(arrival) is dict
                        and set(arrival) == {"offset", "monotonic"}
                        and type(arrival["offset"]) is int
                        and offset < arrival["offset"] <= raw["size"]
                        and type(arrival["monotonic"]) in (int, float)
                        and math.isfinite(arrival["monotonic"])
                        and observed <= arrival["monotonic"],
                        "invalid live diagnostics arrival order",
                    )
                    offset, observed = arrival["offset"], arrival["monotonic"]
            elif result["status"] == "passed":
                _require(
                    "live-stop.json" in diagnostics,
                    "successful diagnostics missing local capture stop",
                )
            continue
        _require(
            type(identity.get("pid")) is int
            and 0 < identity["pid"] <= 2147483647
            and identity.get("serial") == result["device"]
            and identity.get("package") == result["app_id"]
            and argv is not None
            and argv[3:]
            == [
                "logcat",
                "-d",
                "-v",
                "threadtime",
                "--pid",
                str(identity["pid"]),
                "-t",
                "1000",
                "BleHubBlengClose:D",
                "ReactNativeJS:V",
                "*:S",
            ],
            "Close diagnostics original PID/tag selector conflict",
        )
        captures.append(name)
    if result["status"] != "passed":
        return

    def receipt(name: str, status: str = "passed") -> dict:
        doc = documents.get(name)
        _require(
            doc is not None
            and doc.get("run_id") == result["run_id"]
            and doc.get("status") == status,
            f"missing/failed same-run receipt: {name}",
        )
        return doc

    _require(
        prepared is not None and "candidate.apk" in by_name,
        "missing prepared result or frozen APK",
    )
    _require(
        not any(
            name.endswith("-failed.json")
            or doc.get("status") in ("failed", "quarantined")
            or doc.get("error") is not None
            for name, doc in documents.items()
            if name != "prepared-result.json"
        ),
        "failed owner evidence cannot become success",
    )
    final = receipt("candidate/finalization.json")
    app = final.get("identity")
    _require(
        final.get("action") == "retain" and type(app) is dict and identity == app,
        "success requires retained bound App identity",
    )
    _require(
        all(
            type(app.get(field)) is int and app[field] > 0
            for field in ("pid", "uid", "start_ticks")
        )
        and app.get("cmdline") == result["app_id"]
        and type(app.get("boot_id")) is str
        and bool(app["boot_id"])
        and type(app.get("base_apk_path")) is str
        and app["base_apk_path"].startswith("/data/app/"),
        "invalid recorded App OS identity",
    )
    if result["scope"] in (IDLE_CONTROL_SCOPE, START_HOLD_CONTROL_SCOPE):
        _require(
            (attempt / diagnostic_prefix).is_dir(),
            "automation control requires explicit diagnostic evidence",
        )
    if (attempt / diagnostic_prefix).is_dir():
        restored = diagnostics.get("restored.json", {})
        original = restored.get("value")
        _require(
            restored.get("run_id") == result["run_id"]
            and restored.get("verified") is True
            and type(original) is str
            and original
            in {
                "",
                "V",
                "D",
                "I",
                "W",
                "E",
                "F",
                "A",
                "S",
                "VERBOSE",
                "DEBUG",
                "INFO",
                "WARN",
                "ERROR",
                "FATAL",
                "ASSERT",
                "SILENT",
                "SUPPRESS",
            },
            "missing/unverified Close diagnostics restoration or invalid value",
        )
        commands = sorted(
            name
            for name in diagnostics
            if re.fullmatch(r"[0-9]{3}-command\.json", name)
        )
        _require(
            len(commands) >= 7
            and commands
            == [f"{index:03d}-command.json" for index in range(1, len(commands) + 1)]
            and sorted(captures) == commands[3:-3],
            "incomplete Close diagnostics prepare/capture/restore receipts",
        )
        tag = "log.tag.BleHubBlengClose"
        expected = [
            ["shell", "getprop", tag],
            ["shell", "setprop", tag, "DEBUG"],
            ["shell", "getprop", tag],
            *[diagnostics[name]["argv"][3:] for name in sorted(captures)],
            ["shell", "getprop", tag],
            ["shell", "setprop", tag, original or '""'],
            ["shell", "getprop", tag],
        ]
        for name, args in zip(commands, expected, strict=True):
            doc = diagnostics[name]
            stdout = doc.get("stdout")
            _require(
                doc.get("run_id") == result["run_id"]
                and type(doc.get("argv")) is list
                and doc["argv"][3:] == args
                and type(doc.get("returncode")) is int
                and doc["returncode"] == 0
                and type(stdout) is str
                and type(doc.get("stdout_bytes")) is int
                and doc["stdout_bytes"] == len(stdout.encode("utf-8")),
                "failed/mismatched Close diagnostics command receipt",
            )
        values = [diagnostics[name]["stdout"].removesuffix("\n") for name in commands]
        _require(
            values[0] == values[-1] == original
            and values[2] == "DEBUG"
            and values[-3] in {original, "DEBUG"},
            "Close diagnostics original/DEBUG/restoration readback conflict",
        )
        intents = {
            name
            for name in diagnostics
            if name.endswith("-intent.json") and name != "live-intent.json"
        }
        expected_intents = {
            commands[index].replace("-command", "-intent")
            for index in (1, len(commands) - 2)
        }
        _require(
            intents == expected_intents, "missing Close diagnostics mutation intent"
        )
        for name in intents:
            intent = diagnostics[name]
            command = diagnostics[name.replace("-intent", "-command")]
            _require(
                intent.get("run_id") == result["run_id"]
                and intent.get("argv") == command["argv"]
                and intent.get("original_value") == original,
                "Close diagnostics intent original value/command conflict",
            )
    installs = [
        name
        for name in documents
        if re.fullmatch(r"candidate/[0-9]{3}-install\.json", name)
    ]
    _require(len(installs) == 1, "exactly one known candidate install receipt required")
    install = receipt(installs[0])
    log = installs[0][:-5] + ".log"
    _require(
        log in by_name
        and install.get("stdout_sha256") == by_name[log]["sha256"]
        and install.get("serial") == result["device"]
        and install.get("apk_sha256") == result["apk_sha256"]
        and install.get("grant") is False,
        "install receipt/log conflict",
    )
    start = receipt("automation/start.json")
    close = receipt("automation/close.json")
    _require(
        start.get("platform") == "android"
        and start.get("device") == result["device"]
        and start.get("app_id") == result["app_id"]
        and type(start.get("session_id")) is str
        and bool(start["session_id"]),
        "invalid automation start identity",
    )
    _require(
        close.get("session_id") == start["session_id"]
        and close.get("session_closed") is True
        and close.get("server_closed") is True
        and close.get("platform_backend_cleanup") == "passed"
        and close.get("helper_restoration") == "passed"
        and close.get("errors") == [],
        "incomplete automation cleanup",
    )
    _require(
        bound is not None
        and bound.get("run_id") == result["run_id"]
        and bound.get("host_session_id") == start["session_id"],
        "missing bound platform host session",
    )
    platform = receipt("platform/finalized.json")
    _require(
        platform.get("session_deleted") is True
        and platform.get("host_gate_status") == "pending"
        and platform.get("resource_zero") == "not-claimed",
        "invalid platform finalization",
    )
    helpers = receipt("helpers/restored.json", "restored")
    _require(
        helpers.get("preparation_error") is None
        and helpers.get("host_gate_status") == "pending"
        and helpers.get("phone_released") is False,
        "invalid helper restoration",
    )
    receipt("phone.json")
    _require(
        phone.get("primary_error") is None
        and phone.get("platform") == "android"
        and phone.get("device") == result["device"],
        "phone journal cannot support success",
    )


def _summary(result: dict) -> dict:
    return {
        field: result[field]
        for field in (
            "schema_version",
            "scope",
            "run_id",
            "platform",
            "device",
            "app_id",
            "apk_sha256",
            "scenario_status",
            "session_status",
            "host_gate_status",
            "phone_status",
            "status",
            "rf",
        )
    }


def publish_lifecycle_bundle(attempt: Path, result: dict) -> Path:
    """Exclusively publish result.json and manifest.json after phone finalization.

    Failure never overwrites old evidence or edits the phone journal. A partial
    result without a manifest is not a sealed bundle and cannot verify offline.
    """
    deadline = time.monotonic() + MAX_SECONDS
    attempt = _path(attempt)
    # Serialize/detach first, rejecting non-JSON input and nonfinite values.
    raw = json.dumps(result, allow_nan=False).encode()
    _require(len(raw) <= MAX_JSON_BYTES, "result exceeds JSON limit")
    result = _strict_json(raw)
    _require(
        not os.path.lexists(attempt / "result.json")
        and not os.path.lexists(attempt / "manifest.json"),
        "result/manifest already exists",
    )
    before = _snapshot(attempt, deadline)
    _facts(attempt, result, before[0], deadline)
    _require(
        before == _snapshot(attempt, deadline), "evidence drift before publication"
    )
    result_owned = []
    result_digest = publish_new_json(
        attempt / "result.json", result, on_commit=result_owned.append
    )
    sealed = _snapshot(attempt, deadline)
    result_row = next(row for row in sealed[0] if row["path"] == "result.json")
    _require(
        result_row["sha256"] == result_digest
        and sealed[2]["result.json"][:2] == result_owned[0],
        "result bytes/inode drift during publication",
    )
    _require(
        [row for row in sealed[0] if row["path"] != "result.json"] == before[0]
        and sealed[1] == before[1]
        and {key: value for key, value in sealed[2].items() if key != "result.json"}
        == before[2],
        "evidence drift during result publication",
    )
    manifest = {**_summary(result), "files": sealed[0], "directories": sealed[1]}
    _require(
        len(json.dumps(manifest).encode()) <= MAX_JSON_BYTES,
        "manifest exceeds JSON limit",
    )
    _require(
        sealed == _snapshot(attempt, deadline),
        "evidence drift before manifest publication",
    )
    owned = []
    try:
        manifest_path = attempt / "manifest.json"
        manifest_digest = publish_new_json(
            manifest_path, manifest, on_commit=owned.append
        )
        manifest_signature = _sig(manifest_path.lstat())
        _require(
            manifest_signature[:2] == owned[0]
            and _read(manifest_path, limit=MAX_JSON_BYTES, deadline=deadline)[0]
            == manifest_digest,
            "manifest bytes/inode drift during publication",
        )
        _require(
            sealed == _snapshot(attempt, deadline),
            "evidence drift after manifest publication",
        )
        _require(
            _verify(attempt, deadline) == result,
            "published lifecycle replay mismatch",
        )
        _require(
            _sig(manifest_path.lstat()) == manifest_signature
            and _read(manifest_path, limit=MAX_JSON_BYTES, deadline=deadline)[0]
            == manifest_digest,
            "manifest drift after publication replay",
        )
    except BaseException as error:
        try:
            path = attempt / "manifest.json"
            if (
                owned
                and path.exists()
                and (path.lstat().st_dev, path.lstat().st_ino) == owned[0]
            ):
                path.unlink()
        except OSError as cleanup:
            error.add_note(f"manifest retraction: {cleanup}")
        raise
    return attempt / "result.json"


def verify_lifecycle_bundle(attempt: Path) -> dict:
    """Replay recorded facts/integrity only; never acquire or restore authority."""
    return _verify(_path(attempt), time.monotonic() + MAX_SECONDS)


def _verify(attempt: Path, deadline: float) -> dict:
    manifest_path = attempt / "manifest.json"
    manifest_signature = _sig(manifest_path.lstat())
    manifest_digest, _, raw = _read(
        manifest_path, limit=MAX_JSON_BYTES, deadline=deadline
    )
    manifest = _strict_json(raw)
    before = _snapshot(attempt, deadline)
    result = _json(attempt / "result.json", deadline)
    _result(result)
    expected = {**_summary(result), "files": before[0], "directories": before[1]}
    # Typed JSON equality: Python equality alone conflates true/1 and 1.0/1.
    _require(
        json.dumps(manifest, sort_keys=True) == json.dumps(expected, sort_keys=True),
        "manifest inventory/summary mismatch (extra, missing or changed evidence)",
    )
    _facts(attempt, result, before[0], deadline)
    _require(
        before == _snapshot(attempt, deadline)
        and _sig(manifest_path.lstat()) == manifest_signature
        and _read(manifest_path, limit=MAX_JSON_BYTES, deadline=deadline)[0]
        == manifest_digest,
        "bundle drift during offline replay",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify lifecycle bundle offline integrity only; no device commands, "
            "RF execution or host gate upgrade."
        )
    )
    parser.add_argument(
        "attempt", type=Path, help="recorded lifecycle attempt directory"
    )
    args = parser.parse_args(argv)
    try:
        result = verify_lifecycle_bundle(args.attempt)
        summary = {
            "verification": "offline_integrity",
            "integrity_status": "passed",
            "host_gate_upgraded": False,
            **_summary(result),
        }
        print(json.dumps(summary, sort_keys=True))
    except Exception as error:  # noqa: BLE001 -- CLI failures return 2; interruptions propagate.
        print(
            f"lifecycle offline integrity verification failed: {error}", file=sys.stderr
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
