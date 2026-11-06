"""Host-submitted RF scenario evidence, not signing or installed-binary proof.

Runners must install ONLY the Path returned by prepare_artifact(), call
verify_artifact() immediately before installation, and call finish() after their
normal RF validation and cleanup. Constructor/publication failures are runner
errors too. Preserve a primary exception and add a note if finish itself raises.
Offline verification checks integrity/structure only; it never authorizes RF.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform as host_platform
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from tools.react_native import evidence_files
from tools.react_native.evidence_files import signature as _signature
from tools.react_native.host_contract import ANDROID_PACKAGE, BUNDLE_ID, SCHEME

MANIFEST = "evidence-bundle.json"
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024 - 1
MAX_ENTRIES = 20_000
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MODES = (
    "rf",
    "rf-connect",
    "rf-gatt",
    "rf-io",
    "rf-rssi",
    "rf-notification",
    "rf-indication",
    "rf-passive-disconnect",
    "rf-cancellation",
    "rf-watchdog",
)
SCENARIO_CATALOG = {
    "continuous10": MODES,
    "full-suite": (),
    "pending-read-reload": ("generation-1", "generation-2"),
    "recovery-ten": tuple(f"cycle-{cycle}" for cycle in range(10)),
    "write-reject": ("response-write-reject",),
    "ccc-enable-reject": ("ccc-enable-reject",),
    "ccc-disable-reject": ("ccc-disable-reject",),
    "adapter-transition": ("off", "on"),
    "ios-adapter-startup": ("off", "on"),
    "permission-transition": ("denied", "active", "restored"),
}
PERMISSION_FILE_BYTES = 8 * 1024 * 1024
PERMISSION_PROOF_BYTES = 32 * 1024 * 1024
FULL_PREFIX = "BLEHUB_NITRO_RUNTIME rf-lab-full:"
RAW_PREFIX = "BLEHUB_NITRO_RUNTIME "
RECOVERY_PREFIX = "BLEHUB_NITRO_RUNTIME rf-recovery-ten:"
RECOVERY_PHASES = ("baseline", "active", "passive", "recovery-disconnected", "closed")
# Private spec: integrations/react-native-internal/specs/BleHubProbe.nitro.ts.
RECOVERY_RESOURCE_FIELDS = (
    "activeScans",
    "connections",
    "gattAttributes",
    "subscriptions",
    "pendingIo",
    "watchdogs",
    "callbackRoutes",
    "ingressRoots",
    "adapterDelegates",
    "peerCacheEntries",
    "backendLeases",
    "hubRoots",
    "handoffRoots",
    "handoffPending",
    "listeners",
    "workerThreads",
    "lifecycleOwners",
)
RECOVERY_RUNTIME = re.compile(
    r"runtime:runId=([^:\s]+):nonce=([^:\s]+):phase=(start|close)"
)
RECOVERY_CYCLE = re.compile(r"cycle:([0-9]):(start|passed)")
RECOVERY_RESOURCES = re.compile(
    r"resources:cycle=([0-9]):phase=(baseline|active|passive|recovery-disconnected|closed):snapshot=(.*)"
)
RECOVERY_SUMMARY = re.compile(r"(passed|failed):cycles=([0-9]+):runId=([^:\s]+)")
RECOVERY_NATIVE = re.compile(r"native:(bootstrap|signal):(.*)")
PENDING_PREFIX = "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:"
PENDING_RUNTIME = re.compile(
    r"generation=([12]):runtime-start:runId=([^:\s]+):nonce=([^:\s]+)"
)
PENDING_READY = re.compile(r"generation=([12]):native-ready:passed")
PENDING_OLD_READ = re.compile(
    r"generation=1:read-dispatched:commandId=[0-9]+:connectionId=[0-9]+:"
    r"gattSessionId=[0-9]+:characteristicId=[0-9]+:peripheralId=[0-9]+"
)
PENDING_READ = re.compile(
    r"generation=([12]):read:passed:commandId=[0-9]+:connectionId=[0-9]+:"
    r"gattSessionId=[0-9]+:characteristicId=[0-9]+:value=[0-9a-fA-F]*"
)
PENDING_CLOSE = re.compile(r"generation=([12]):close:passed")
PENDING_SUMMARY = re.compile(r"passed:runId=([^:\s]+)")
PENDING_NATIVE = re.compile(
    r"native:(read-accepted|engine-invalidation-close-started|dispose-returned|"
    r"engine-close-complete|late-submit-rejected):runId=([^:\s]+):probeId=([^:\s]+):"
    r"commandId=[0-9]+:connectionId=[0-9]+:gattSessionId=[0-9]+:characteristicId=[0-9]+"
    r"(?::source=synthetic:result=closed)?"
)
PREFIX = "BLEHUB_NITRO_RUNTIME rf-lab-suite:"
FLOW = re.compile(
    r"flow-(started|passed|failed):index=([0-9]+):mode=([^\s:]+)(?::.*)?$"
)
RUNTIME = re.compile(
    r"runtime:session=([0-9a-f-]+):nonce=([0-9a-z-]+):phase=(start|close)$"
)
SUMMARY = re.compile(
    r"(passed|failed):flows=10:passedCount=([0-9]+):failureCount=([0-9]+)$"
)
MACHO_MAGICS = {
    bytes.fromhex(value)
    for value in (
        "feedface",
        "cefaedfe",
        "feedfacf",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
}
LIMITATIONS = [
    "Artifact fingerprints describe the host-submitted installation snapshot, not the installed binary hash.",
    "This is not signing/security proof or source-to-build attestation; dirty source cannot prove source-to-binary correspondence.",
    "Physical BLE assertions remain the runner's authority; offline integrity verification does not reauthorize RF.",
    (
        "Extended attributes (xattrs) and timestamps are intentionally excluded from snapshot copying "
        "and artifact content fingerprints; source stat timestamps are used only for cooperative drift detection. "
        "Bytes and modes are preserved, not whole-filesystem equivalence."
    ),
]


class EvidenceError(ValueError):
    """Evidence cannot support the requested integrity/structural claim."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_name(name: str) -> str:
    return evidence_files.safe_name(name, require=_require)


def _no_links(path: Path) -> None:
    evidence_files.no_links(path, require=_require)


def _open_regular(path: Path) -> BinaryIO:
    _no_links(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode), f"not a regular file: {path}")
    _require(before.st_size <= MAX_FILE_BYTES, f"oversized file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    if _signature(os.fstat(stream.fileno())) != _signature(before):
        stream.close()
        raise EvidenceError(f"file changed at open: {path}")
    return stream


def _hash_stream(stream: BinaryIO, limit: int = MAX_FILE_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(CHUNK_BYTES):
        size += len(block)
        _require(size <= limit, "stream exceeds byte limit")
        digest.update(block)
    return digest.hexdigest(), size


def _file(path: Path, relative: str) -> dict[str, Any]:
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        digest, size = _hash_stream(stream)
        after = os.fstat(stream.fileno())
    _require(
        _signature(before) == _signature(after) == _signature(path.lstat())
        and size == before.st_size,
        f"file changed while hashing: {path}",
    )
    return {
        "path": relative,
        "sha256": digest,
        "size": size,
        "mode": stat.S_IMODE(before.st_mode),
    }


def _read_small(path: Path, limit: int = MAX_DOCUMENT_BYTES) -> bytes:
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        _require(before.st_size <= limit, f"oversized document: {path}")
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    _require(
        len(content) <= limit
        and len(content) == before.st_size
        and _signature(before) == _signature(after) == _signature(path.lstat()),
        f"document changed while reading: {path}",
    )
    return content


def _json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            _require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(_read_small(path), object_pairs_hook=unique)
    _require(isinstance(value, dict), f"JSON must be an object: {path}")
    return value


def _publish(path: Path, value: dict[str, Any]) -> None:
    # Same exclusive temporary + replace pattern as cancellation_control.write_document.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _walk(
    root: Path, *, evidence: bool = False
) -> list[tuple[str, Path, os.stat_result]]:
    _no_links(root)
    _require(stat.S_ISDIR(root.lstat().st_mode), f"not a directory: {root}")
    result: list[tuple[str, Path, os.stat_result]] = []
    aliases: set[str] = set()
    total = 0
    entry_count = 0

    def visit(directory: Path) -> None:
        nonlocal total, entry_count
        with os.scandir(directory) as entries:
            bounded_entries = []
            for entry in entries:
                entry_count += 1
                _require(entry_count <= MAX_ENTRIES, "too many entries")
                bounded_entries.append(entry)
            for entry in sorted(bounded_entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if evidence and relative in (MANIFEST, "candidate"):
                    continue
                _safe_name(relative)
                _require(
                    relative.casefold() not in aliases, f"aliased path: {relative}"
                )
                aliases.add(relative.casefold())
                info = entry.stat(follow_symlinks=False)
                _require(
                    stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                    f"symlink/special entry: {relative}",
                )
                result.append((relative, path, info))
                _require(len(result) <= MAX_ENTRIES, "too many entries")
                if stat.S_ISDIR(info.st_mode):
                    visit(path)
                else:
                    _require(
                        info.st_size <= MAX_FILE_BYTES, f"oversized file: {relative}"
                    )
                    total += info.st_size
                    _require(total <= MAX_TOTAL_BYTES, "total bytes exceed limit")

    visit(root)
    return sorted(result, key=lambda item: item[0])


def _source_signature(path: Path) -> list[Any]:
    _no_links(path)
    info = path.lstat()
    if stat.S_ISREG(info.st_mode):
        return [("", _signature(info))]
    return [
        ("", _signature(info)),
        *[(name, _signature(item)) for name, _, item in _walk(path)],
    ]


def _copy_file(source: Path, destination: Path) -> None:
    with _open_regular(source) as input_stream, destination.open("xb") as output:
        before = os.fstat(input_stream.fileno())
        size = 0
        while block := input_stream.read(CHUNK_BYTES):
            size += len(block)
            _require(size <= MAX_FILE_BYTES, "copy exceeds file limit")
            output.write(block)
        after = os.fstat(input_stream.fileno())
    _require(
        _signature(before) == _signature(after) == _signature(source.lstat())
        and size == before.st_size,
        f"source changed while copying: {source}",
    )
    destination.chmod(stat.S_IMODE(before.st_mode))


def _zip_directory_bound(stream: BinaryIO) -> int:
    """Bound central-directory allocation BEFORE ZipFile parses its inventory.

    APKs below our size/count limits need no ZIP64 or multi-disk containers.
    The APK signing block (before the directory) remains part of the raw hash.
    """
    return evidence_files.zip_directory_bound(
        stream, max_entries=MAX_ENTRIES, require=_require
    )


def _apk(path: Path) -> dict[str, Any]:
    _require(path.suffix.lower() == ".apk", "Android artifact must be an APK")
    archive_fingerprint = _file(path, path.name)
    # Keep the verified descriptor open: never load the full archive into memory.
    with _open_regular(path) as raw:
        expected_entries = _zip_directory_bound(raw)
        return _apk_inventory(path, raw, archive_fingerprint, expected_entries)


def _apk_inventory(
    path: Path,
    raw: BinaryIO,
    archive_fingerprint: dict[str, Any],
    expected_entries: int,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    names: set[str] = set()
    total = 0
    with zipfile.ZipFile(raw) as archive:
        entries = archive.infolist()
        _require(len(entries) <= MAX_ENTRIES, "too many APK entries")
        _require(len(entries) == expected_entries, "bad ZIP directory entry count")
        for entry in entries:
            name = _safe_name(entry.filename[:-1] if entry.is_dir() else entry.filename)
            _require(entry.orig_filename == entry.filename, "NUL/altered ZIP path")
            # ZIP names are case-sensitive; the APK is copied, never extracted.
            # Removing a directory's trailing slash still catches file/dir conflicts.
            _require(name not in names, f"duplicate APK entry: {name}")
            names.add(name)
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            _require(
                kind in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG),
                f"symlink/special APK entry: {name}",
            )
            _require(not entry.flag_bits & 1, f"encrypted APK entry: {name}")
            total += entry.file_size
            _require(
                0 <= entry.file_size <= MAX_FILE_BYTES and total <= MAX_TOTAL_BYTES,
                "APK expanded size exceeds limit",
            )
            with archive.open(entry) as stream:
                digest, size = _hash_stream(stream)
            _require(
                size == entry.file_size and (not entry.is_dir() or size == 0),
                f"bad ZIP entry size: {name}",
            )
            items.append(
                {
                    "path": name,
                    "sha256": digest,
                    "size": size,
                    "type": "directory" if entry.is_dir() else "file",
                }
            )
    files = {item["path"] for item in items if item["type"] == "file"}
    _require("assets/index.android.bundle" in files, "missing Android JS bundle")
    dex = sorted(
        name for name in files if re.fullmatch(r"classes(?:[1-9][0-9]*)?\.dex", name)
    )
    _require(bool(dex), "missing classes*.dex")
    libraries = sorted(
        name for name in files if re.fullmatch(r"lib/[^/]+/[^/]+\.so", name)
    )
    abis = {name.split("/")[1] for name in libraries}
    _require(bool(abis), "missing native libraries")
    for abi in abis:
        for library in ("libblehub_uniffi.so", "libBleHubReactNative.so"):
            _require(f"lib/{abi}/{library}" in files, f"missing {library} for {abi}")
    _require(
        not any(
            "blehub" in name.rsplit("/", 1)[1].lower()
            and name.rsplit("/", 1)[1]
            not in ("libblehub_uniffi.so", "libBleHubReactNative.so")
            for name in libraries
        ),
        "unexpected BleHub native image",
    )
    items.sort(key=lambda item: item["path"])
    _require(
        archive_fingerprint == _file(path, path.name), "APK changed during inspection"
    )
    return {
        "kind": "apk",
        "archive": archive_fingerprint,
        "items": items,
        "items_sha256": _digest(items),
        "js": ["assets/index.android.bundle"],
        "dex": dex,
        "native_images": libraries,
    }


def _app(path: Path) -> dict[str, Any]:
    _require(path.suffix == ".app", "iOS artifact must be an .app directory")
    before = _source_signature(path)
    rows = _walk(path)
    items: list[dict[str, Any]] = []
    native: list[str] = []
    for name, item, info in rows:
        if stat.S_ISDIR(info.st_mode):
            items.append(
                {"path": name, "type": "directory", "mode": stat.S_IMODE(info.st_mode)}
            )
        else:
            items.append({**_file(item, name), "type": "file"})
            with _open_regular(item) as stream:
                if stream.read(4) in MACHO_MAGICS:
                    native.append(name)
    document = plistlib.loads(_read_small(path / "Info.plist", 1024 * 1024))
    _require(isinstance(document, dict), "invalid Info.plist")
    bundle_id = document.get("CFBundleIdentifier")
    _require(
        bundle_id == BUNDLE_ID,
        f"CFBundleIdentifier must match RF host {BUNDLE_ID}: {bundle_id!r}",
    )
    executable = document.get("CFBundleExecutable")
    _safe_name(executable)
    _require(
        "/" not in executable and executable in native,
        "missing Mach-O CFBundleExecutable",
    )
    _require(
        bool((path / executable).lstat().st_mode & 0o111),
        "iOS executable lacks execute mode",
    )
    _require(
        any(
            item["path"] == "main.jsbundle" and item["type"] == "file" for item in items
        ),
        "missing main.jsbundle",
    )
    _require(before == _source_signature(path), "app changed during inspection")
    return {
        "kind": "app-tree",
        "root_mode": stat.S_IMODE(path.lstat().st_mode),
        "items": items,
        "tree_sha256": _digest(items),
        "bundle_id": bundle_id,
        "executable": executable,
        "js": ["main.jsbundle"],
        "native_images": native,
    }


def _artifact(path: Path, platform: str) -> dict[str, Any]:
    _require(platform in ("android", "ios"), "unsupported platform")
    return _apk(path) if platform == "android" else _app(path)


def _inventory(attempt: Path) -> list[dict[str, Any]]:
    return [
        _file(path, name)
        for name, path, info in _walk(attempt, evidence=True)
        if stat.S_ISREG(info.st_mode)
    ]


def _validate_scenario(platform: str, scenario: str) -> None:
    _require(platform in ("android", "ios"), "unsupported platform")
    _require(
        scenario not in ("adapter-transition", "permission-transition")
        or platform == "android",
        f"{scenario} is Android-only",
    )
    _require(
        scenario != "ios-adapter-startup" or platform == "ios",
        "ios-adapter-startup is iOS-only",
    )
    _require(
        isinstance(scenario, str) and scenario in SCENARIO_CATALOG,
        f"unsupported/missing scenario: {scenario!r}",
    )


def _empty_index(relative: str, scenario: str) -> dict[str, Any]:
    if scenario == "ios-adapter-startup":
        from tools.react_native.ios_adapter_startup_evidence import index_runtime

        return index_runtime("", relative)
    if scenario in ("write-reject", "ccc-enable-reject", "ccc-disable-reject"):
        return {
            "scenario": scenario,
            "source": relative,
            "sha256": None,
            "size": 0,
            "facts": None,
            "invalid_markers": [],
            "error_markers": [],
        }
    if scenario == "permission-transition":
        from tools.react_native.permission_transition_ledger import index_runtime

        return index_runtime("", relative)
    if scenario == "adapter-transition":
        from tools.react_native.adapter_transition_ledger import index_runtime

        return index_runtime("", relative)
    if scenario == "full-suite":
        return {
            "scenario": scenario,
            "continuous10": _empty_index(relative, "continuous10"),
            "recovery_ten": _empty_index(relative, "recovery-ten"),
            "sub_status": {
                "continuous10": "not-started",
                "recovery_ten": "not-started",
            },
            "events": [],
            "native_bootstraps": [],
            "invalid_markers": [],
            "error_markers": [],
        }
    flows = []
    for index, mode in enumerate(SCENARIO_CATALOG[scenario]):
        row = {
            "index": index,
            "mode": mode,
            "status": "missing",
            "source": relative,
            "start_line": None,
            "end_line": None,
            "sha256": None,
            "size": 0,
        }
        if scenario == "pending-read-reload":
            row["generation"] = index + 1
        elif scenario == "recovery-ten":
            row["cycle"] = index
        flows.append(row)
    result = {
        "flows": flows,
        "events": [],
        "runtime_markers": [],
        "summaries": [],
        "invalid_markers": [],
    }
    if scenario in ("pending-read-reload", "recovery-ten"):
        result.update(scenario=scenario, error_markers=[])
    if scenario == "recovery-ten":
        result.update(
            mode="cycle", prefix=RECOVERY_PREFIX, resources=[], native_markers=[]
        )
    return result


def _flow_index(
    path: Path, relative: str, scenario: str = "continuous10"
) -> dict[str, Any]:
    _require(scenario in SCENARIO_CATALOG, "unsupported scenario")
    if scenario == "ios-adapter-startup":
        from tools.react_native.ios_adapter_startup_evidence import index_runtime

        return index_runtime(_read_small(path, MAX_LOG_BYTES).decode("utf-8"), relative)
    if scenario == "write-reject":
        return _write_reject_index(path, relative)
    if scenario == "ccc-enable-reject":
        return _ccc_enable_reject_index(path, relative)
    if scenario == "ccc-disable-reject":
        return _ccc_disable_reject_index(path, relative)
    if scenario == "permission-transition":
        return _permission_index(path, relative)
    if scenario == "adapter-transition":
        from tools.react_native.adapter_transition_ledger import index_runtime

        # Adapter phase ranges always bind the unchanged raw capture, even when
        # runtime_log is the host's normalized marker projection. On failure a
        # partial projection remains indexable before raw capture is available.
        root = path.parents[len(PurePosixPath(relative).parts) - 1]
        raw_name = "adapter-transition-runtime.raw.log"
        if (root / raw_name).exists():
            path, relative = root / raw_name, raw_name
        return index_runtime(_read_small(path, MAX_LOG_BYTES).decode("utf-8"), relative)
    if scenario == "full-suite":
        return _full_index(path, relative)
    if scenario == "pending-read-reload":
        return _pending_index(path, relative)
    if scenario == "recovery-ten":
        return _recovery_index(path, relative)
    return _continuous_index(path, relative)


def _write_reject_index(path: Path, relative: str) -> dict[str, Any]:
    from tools.react_native import write_reject_ledger

    return _reject_index(
        path, relative, "write-reject", write_reject_ledger.runtime_facts
    )


def _ccc_enable_reject_index(path: Path, relative: str) -> dict[str, Any]:
    from tests.peripherals.nrf52840 import ccc_enable_reject as wire

    return _reject_index(path, relative, wire.SCENARIO, wire.runtime_facts)


def _ccc_disable_reject_index(path: Path, relative: str) -> dict[str, Any]:
    from tests.peripherals.nrf52840 import ccc_disable_reject as wire

    return _reject_index(path, relative, wire.SCENARIO, wire.runtime_facts)


def _reject_index(
    path: Path, relative: str, scenario: str, runtime_facts: Any
) -> dict[str, Any]:
    from tests.peripherals.nrf52840.fixture_session import FixtureSessionError

    content = _read_small(path, MAX_LOG_BYTES)
    result = _empty_index(relative, scenario)
    result.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    try:
        text = content.decode("utf-8")
        start = re.search(rf"rf-{re.escape(scenario)}:runtime:session=([^:\s]+):", text)
        _require(start is not None, f"{scenario} missing runtime start")
        result["facts"] = runtime_facts(text, run_id=start[1])
    except (ValueError, FixtureSessionError) as error:
        # Failed attempts retain their original bytes and parser diagnosis.
        result["error_markers"].append(str(error))
    return result


def _recovery_json(content: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            _require(key not in result, f"duplicate recovery JSON key: {key}")
            result[key] = value
        return result

    # Preserve negative zero so it cannot silently become an allowed zero count.
    def integer(value: str) -> int | float:
        return -0.0 if value == "-0" else int(value)

    def constant(value: str) -> Any:
        raise EvidenceError(f"invalid recovery JSON constant: {value}")

    document = json.loads(
        content, object_pairs_hook=unique, parse_int=integer, parse_constant=constant
    )
    _require(isinstance(document, dict), "recovery marker JSON must be an object")
    return document


def _recovery_index(
    path: Path, relative: str, *, stop_line: int | None = None, full_suite: bool = False
) -> dict[str, Any]:
    """Ten inclusive raw cycle segments, extending to next start/EOF.

    Snapshots are wire projections of real counters, not heap/OS/security proof.
    Native bootstrap/signal JSON and BLE event semantics belong to the parent
    checker. We retain their raw file hash and marker positions, not SDK identity
    authority or a second ACK/BLE checker.
    """
    result = _empty_index(relative, "recovery-ten")
    occurrences: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    digest = hashlib.sha256()
    line_number = 0
    records = 0
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        _require(before.st_size <= MAX_LOG_BYTES, "runtime log exceeds limit")
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            _require(len(raw) <= MAX_LINE_BYTES, "runtime log line exceeds limit")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if stop_line is not None and line_number > stop_line:
                line_number -= 1
                break
            if not full_suite and (FULL_PREFIX in text or PREFIX in text):
                result["invalid_markers"].append(line_number)
            body = text.split(RECOVERY_PREFIX, 1)[1] if RECOVERY_PREFIX in text else ""
            complete = raw.endswith(b"\n")
            cycle = RECOVERY_CYCLE.fullmatch(body) if complete else None
            runtime = RECOVERY_RUNTIME.fullmatch(body) if complete else None
            resources = RECOVERY_RESOURCES.fullmatch(body) if complete else None
            summary = RECOVERY_SUMMARY.fullmatch(body) if complete else None
            native = RECOVERY_NATIVE.fullmatch(body) if complete else None
            if cycle is not None and cycle[2] == "start":
                if active is not None:
                    active.update(end_line=line_number - 1, sha256=digest.hexdigest())
                number = int(cycle[1])
                active = {
                    "index": number,
                    "cycle": number,
                    "mode": f"cycle-{number}",
                    "status": "partial",
                    "source": relative,
                    "start_line": line_number,
                    "size": 0,
                }
                occurrences.append(active)
                digest = hashlib.sha256()
            if active is not None:
                digest.update(raw)
                active["size"] += len(raw)
            if body:
                records += 1
                _require(records <= MAX_ENTRIES, "too many recovery-ten markers")
            # Do not mistake strings/fields inside opaque event/native JSON for
            # failure markers. The parent checker owns those event semantics.
            header = body.split("{", 1)[0]
            # A successful passive disconnect includes its expected domain error
            # category. This is not an error marker; exact route/category semantics
            # remain mandatory in the parent ledger.
            if re.fullmatch(
                r"cycle:[0-9]:passive-disconnect:passed:commandId=none:"
                r"connectionId=[0-9]+:error=(none|connection)",
                header,
            ):
                header = header.split(":passed:", 1)[0]
            failed = re.search(
                r"(?:^|:)(?:failed|failure|error|[^:]*-failed)(?:=|:|$)", header
            )
            if failed:
                result["error_markers"].append({"line": line_number})
                if active is not None:
                    active["status"] = "failed"
            if cycle is not None:
                result["events"].append(
                    {"event": cycle[2], "cycle": int(cycle[1]), "line": line_number}
                )
                if (
                    cycle[2] == "passed"
                    and active is not None
                    and active["cycle"] == int(cycle[1])
                ):
                    active["terminal_line"] = line_number
                    if active["status"] != "failed":
                        active["status"] = "passed"
            if runtime is not None:
                result["runtime_markers"].append(
                    {
                        "run_id": runtime[1],
                        "nonce": runtime[2],
                        "phase": runtime[3],
                        "line": line_number,
                    }
                )
            if summary is not None:
                result["summaries"].append(
                    {
                        "status": summary[1],
                        "cycles": int(summary[2]),
                        "run_id": summary[3],
                        "line": line_number,
                    }
                )
            if resources is not None or native is not None:
                try:
                    payload = _recovery_json(resources[3] if resources else native[2])
                except (ValueError, TypeError):
                    # Keep failed partial evidence instead of masking its primary error.
                    result["invalid_markers"].append(line_number)
                else:
                    if resources is not None:
                        result["resources"].append(
                            {
                                "cycle": int(resources[1]),
                                "phase": resources[2],
                                "snapshot": payload,
                                "line": line_number,
                            }
                        )
                    else:
                        result["native_markers"].append(
                            {"kind": native[1], "line": line_number}
                        )
            elif (
                not failed
                and (
                    body.startswith(
                        ("runtime:", "resources:", "native:", "passed:", "failed:")
                    )
                    or re.match(r"cycle:[^:]+:(start|passed)(?=:|$)", body)
                )
                and cycle is None
                and runtime is None
                and summary is None
            ):
                result["invalid_markers"].append(line_number)
        after = os.fstat(stream.fileno())
    _require(
        _signature(before) == _signature(after) == _signature(path.lstat()),
        "runtime log changed during indexing",
    )
    if active is not None:
        active.update(end_line=line_number, sha256=digest.hexdigest())
    missing = [
        row
        for row in result["flows"]
        if not any(seen["cycle"] == row["cycle"] for seen in occurrences)
    ]
    result["flows"] = sorted([*occurrences, *missing], key=lambda row: row["index"])
    return result


def _pending_event(body: str, line: int) -> dict[str, Any] | None:
    """Parse structural milestones, not route equality or physical BLE assertions.

    Native marker payload syntax is recorded only to identify a real close
    boundary; the independent parent ledger validates IDs/teardown semantics.
    """
    event: dict[str, Any] = {"line": line}
    if match := PENDING_RUNTIME.fullmatch(body):
        event.update(
            event="runtime-start",
            generation=int(match[1]),
            run_id=match[2],
            nonce=match[3],
        )
    elif match := PENDING_READY.fullmatch(body):
        event.update(event="native-ready", generation=int(match[1]))
    elif PENDING_OLD_READ.fullmatch(body):
        event.update(event="read-dispatched", generation=1)
    elif match := PENDING_READ.fullmatch(body):
        event.update(event="read-passed", generation=int(match[1]))
    elif match := PENDING_CLOSE.fullmatch(body):
        event.update(event="close-passed", generation=int(match[1]))
    elif match := PENDING_SUMMARY.fullmatch(body):
        event.update(event="passed", run_id=match[1])
    elif match := PENDING_NATIVE.fullmatch(body):
        # The native close route belongs only to the first, retired engine.
        has_suffix = body.endswith(":source=synthetic:result=closed")
        if has_suffix != (match[1] == "late-submit-rejected"):
            return None
        event.update(
            event="native:" + match[1], generation=1, run_id=match[2], probe_id=match[3]
        )
    else:
        return None
    return event


def _pending_index(path: Path, relative: str) -> dict[str, Any]:
    """Two inclusive raw segments, possibly overlapping across engine teardown.

    generation 1 ends ONLY at native engine-close-complete, generation 2 at
    final passed after its JS close. Unfinished segments extend through EOF.
    Never infer a JS generation-1 pass, or remote GATT behavior from synthetic
    late-submit-rejected. Failed logs preserve every start/partial occurrence.
    """
    result = _empty_index(relative, "pending-read-reload")
    occurrences: list[dict[str, Any]] = []
    active: list[tuple[dict[str, Any], Any]] = []
    close_line = 0
    line_number = 0
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        _require(before.st_size <= MAX_LOG_BYTES, "runtime log exceeds limit")
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            _require(len(raw) <= MAX_LINE_BYTES, "runtime log line exceeds limit")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if FULL_PREFIX in text or RECOVERY_PREFIX in text or PREFIX in text:
                result["invalid_markers"].append(line_number)
            body = text.split(PENDING_PREFIX, 1)[1] if PENDING_PREFIX in text else ""
            event = _pending_event(body, line_number) if raw.endswith(b"\n") else None
            generation_match = re.match(r"generation=([0-9]+):", body)
            failed = re.search(
                r"(?:^|:)(?:failed|failure|error|[^:]*-failed)(?:=|:|$)", body
            )
            if failed:
                result["error_markers"].append({"line": line_number})
                for row, _ in active:
                    if generation_match is None or row["generation"] == int(
                        generation_match[1]
                    ):
                        row["status"] = "failed"
            elif event is None and (
                body.startswith(
                    ("native:", "passed:", "failed:", "generation=1:read-dispatched:")
                )
                or re.match(
                    r"generation=[0-9]+:(?:runtime-start|native-ready|read:passed|close:passed|passed)(?=:|$)",
                    body,
                )
            ):
                result["invalid_markers"].append(line_number)
            if event is not None:
                result["events"].append(event)
                _require(
                    len(result["events"]) <= MAX_ENTRIES,
                    "too many pending-reload structural markers",
                )
                if event["event"] == "runtime-start":
                    generation = event["generation"]
                    # Keep at most one active hash per generation on corrupt logs.
                    # Repeated starts remain partial occurrences, never deduplicated.
                    remaining = []
                    for previous, digest in active:
                        if previous["generation"] == generation:
                            previous.update(
                                end_line=line_number - 1, sha256=digest.hexdigest()
                            )
                        else:
                            remaining.append((previous, digest))
                    active = remaining
                    result["runtime_markers"].append(
                        {
                            key: event[key]
                            for key in ("generation", "run_id", "nonce", "line")
                        }
                    )
                    row = {
                        "index": generation - 1,
                        "mode": f"generation-{generation}",
                        "generation": generation,
                        "status": "partial",
                        "source": relative,
                        "start_line": line_number,
                        "size": 0,
                    }
                    occurrences.append(row)
                    active.append((row, hashlib.sha256()))
                elif event["event"] == "close-passed" and event["generation"] == 2:
                    close_line = line_number
                elif event["event"] == "passed":
                    result["summaries"].append(
                        {
                            "status": "passed",
                            "run_id": event["run_id"],
                            "line": line_number,
                        }
                    )
            remaining = []
            for row, digest in active:
                digest.update(raw)
                row["size"] += len(raw)
                terminal = event is not None and (
                    (
                        row["generation"] == 1
                        and event["event"] == "native:engine-close-complete"
                    )
                    or (
                        row["generation"] == 2
                        and event["event"] == "passed"
                        and row["start_line"] < close_line < line_number
                    )
                )
                if terminal:
                    row.update(
                        end_line=line_number,
                        terminal_line=line_number,
                        sha256=digest.hexdigest(),
                    )
                    if row["status"] != "failed":
                        row["status"] = "passed"
                else:
                    remaining.append((row, digest))
            active = remaining
        after = os.fstat(stream.fileno())
    _require(
        _signature(before) == _signature(after) == _signature(path.lstat()),
        "runtime log changed during indexing",
    )
    for row, digest in active:
        row.update(end_line=line_number, sha256=digest.hexdigest())
    missing = [
        row
        for row in result["flows"]
        if not any(seen["generation"] == row["generation"] for seen in occurrences)
    ]
    result["flows"] = sorted([*occurrences, *missing], key=lambda row: row["index"])
    return result


def _continuous_index(
    path: Path, relative: str, *, stop_line: int | None = None, full_suite: bool = False
) -> dict[str, Any]:
    """Hash inclusive, 1-based RAW lines from start through the next start/EOF.

    Keep timestamp/native prefixes and newline bytes, including a partial tail.
    Missing flows have null ranges/hash; duplicate starts remain occurrences and
    are rejected by passed structure validation rather than silently deduplicated.
    """
    occurrences: list[dict[str, Any]] = []
    events: list[list[Any]] = []
    runtimes: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    invalid_markers: list[int] = []
    active: dict[str, Any] | None = None
    digest = hashlib.sha256()
    size = 0
    line_number = 0
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        _require(before.st_size <= MAX_LOG_BYTES, "runtime log exceeds limit")
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            _require(len(raw) <= MAX_LINE_BYTES, "runtime log line exceeds limit")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if stop_line is not None and line_number > stop_line:
                line_number -= 1
                break
            if not full_suite and (FULL_PREFIX in text or RECOVERY_PREFIX in text):
                invalid_markers.append(line_number)
            body = text.split(PREFIX, 1)[1] if PREFIX in text else ""
            complete = raw.endswith(b"\n")
            flow = FLOW.fullmatch(body) if complete else None
            runtime = RUNTIME.fullmatch(body) if complete else None
            summary = SUMMARY.fullmatch(body) if complete else None
            if body.startswith(("flow-", "runtime:", "passed:", "failed:")) and not (
                flow or runtime or summary
            ):
                invalid_markers.append(line_number)
            if flow is not None and flow[1] == "started":
                if active is not None:
                    active.update(
                        end_line=line_number - 1, sha256=digest.hexdigest(), size=size
                    )
                active = {
                    "index": int(flow[2]),
                    "mode": flow[3],
                    "status": "partial",
                    "source": relative,
                    "start_line": line_number,
                }
                occurrences.append(active)
                digest, size = hashlib.sha256(), 0
            if active is not None:
                digest.update(raw)
                size += len(raw)
            if flow is not None:
                event = [flow[1], int(flow[2]), flow[3]]
                events.append(event)
                if (
                    flow[1] != "started"
                    and active is not None
                    and event[1:] == [active["index"], active["mode"]]
                ):
                    active["status"] = flow[1]
                    active["terminal_line"] = line_number
            if runtime is not None:
                runtimes.append(
                    {
                        "session": runtime[1],
                        "nonce": runtime[2],
                        "phase": runtime[3],
                        "line": line_number,
                    }
                )
            if summary is not None:
                summaries.append(
                    {
                        "status": summary[1],
                        "passed_count": int(summary[2]),
                        "failure_count": int(summary[3]),
                        "line": line_number,
                    }
                )
        after = os.fstat(stream.fileno())
    _require(
        _signature(before) == _signature(after) == _signature(path.lstat()),
        "runtime log changed during indexing",
    )
    if active is not None:
        active.update(end_line=line_number, sha256=digest.hexdigest(), size=size)
    missing = [
        {
            "index": index,
            "mode": mode,
            "status": "missing",
            "source": relative,
            "start_line": None,
            "end_line": None,
            "sha256": None,
            "size": 0,
        }
        for index, mode in enumerate(MODES)
        if not any(row["index"] == index and row["mode"] == mode for row in occurrences)
    ]
    return {
        "flows": sorted([*occurrences, *missing], key=lambda row: row["index"]),
        "events": events,
        "runtime_markers": runtimes,
        "summaries": summaries,
        "invalid_markers": invalid_markers,
    }


def _sub_document(index: dict[str, Any], scenario: str) -> dict[str, Any]:
    """Structural sub-verdict only, never a completion or RF authorization."""
    return {
        "completed": True,
        "primary_error": None,
        "cleanup_errors": [],
        "evidence_errors": [],
        "artifact": {},
        "platform": "android",
        "scenario": scenario,
        "flow_index": index,
        "references": {},
    }


def _sub_status(index: dict[str, Any], scenario: str) -> str:
    if not index["runtime_markers"] and all(
        row["status"] == "missing" for row in index["flows"]
    ):
        return "not-started"
    try:
        _passed_structure(_sub_document(index, scenario))
    except (ValueError, KeyError, TypeError):
        return "partial"
    return "passed"


def _full_index(path: Path, relative: str) -> dict[str, Any]:
    """Reuse raw indexers with explicit phase ends and original 1-based lines.

    The launch recovery bootstrap is intentionally outside the recovery runtime.
    No normalized/temp log is indexed; last base flow cannot absorb recovery.
    """
    result = _empty_index(relative, "full-suite")
    result["phase_lines"] = {"continuous10": [], "recovery_ten": []}
    base_end = recovery_end = None
    line_number = 0
    with _open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        _require(before.st_size <= MAX_LOG_BYTES, "runtime log exceeds limit")
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            _require(len(raw) <= MAX_LINE_BYTES, "runtime log line exceeds limit")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.count(RAW_PREFIX) != 1 or not raw.endswith(b"\n"):
                result["invalid_markers"].append(line_number)
                continue
            marker = text.split(RAW_PREFIX, 1)[1]
            _require(line_number <= MAX_ENTRIES, "too many full-suite raw records")
            if marker.startswith("rf-pending-read-reload:"):
                result["invalid_markers"].append(line_number)
            header = marker.split("{", 1)[0]
            if re.search(
                r"(?:^|:)(?:failed|flow-failed|host-failed)(?::|$)|:close:rejected:",
                header,
            ):
                result["error_markers"].append(line_number)
            if marker.startswith("rf-lab-suite:"):
                result["phase_lines"]["continuous10"].append(line_number)
                if SUMMARY.fullmatch(marker.removeprefix("rf-lab-suite:")):
                    base_end = line_number if base_end is None else base_end
            recovery_marker = marker.startswith("rf-recovery-ten:")
            bootstrap = marker.startswith("rf-recovery-ten:native:bootstrap:")
            if recovery_marker and not bootstrap:
                result["phase_lines"]["recovery_ten"].append(line_number)
                if RECOVERY_SUMMARY.fullmatch(marker.removeprefix("rf-recovery-ten:")):
                    recovery_end = line_number if recovery_end is None else recovery_end
            if base_end is None and (
                (recovery_marker and not bootstrap)
                or marker.startswith(
                    ("rf-lab-full:recovery-", "rf-lab-full:native:signal:")
                )
            ):
                base_end = line_number - 1
            if (
                recovery_end is None
                and marker.startswith("rf-lab-full:runtime:")
                and marker.endswith(":phase=close")
            ):
                recovery_end = line_number - 1
            native_prefix = next(
                (
                    p
                    for p in ("rf-recovery-ten:native:bootstrap:", "rf-host:bootstrap:")
                    if marker.startswith(p)
                ),
                None,
            )
            if native_prefix is not None:
                try:
                    value = _recovery_json(marker.removeprefix(native_prefix))
                except (ValueError, TypeError):
                    result["invalid_markers"].append(line_number)
                else:
                    result["native_bootstraps"].append(
                        {"kind": native_prefix, "payload": value, "line": line_number}
                    )
            if not marker.startswith("rf-lab-full:"):
                continue
            body = marker.removeprefix("rf-lab-full:")
            event: dict[str, Any] = {"line": line_number}
            if match := RUNTIME.fullmatch(body):
                event.update(
                    event="runtime-" + match[3], run_id=match[1], nonce=match[2]
                )
            elif match := re.fullmatch(r"recovery-awaiting:runId=([^:\s]+)", body):
                event.update(event="recovery-awaiting", run_id=match[1])
            elif match := re.fullmatch(
                r"recovery-ready:runId=([^:\s]+):source=linking", body
            ):
                event.update(event="recovery-ready", run_id=match[1])
            elif match := re.fullmatch(
                r"passed:flows=10:cycles=10:runId=([^:\s]+)", body
            ):
                event.update(event="passed", run_id=match[1])
            elif body.startswith("native:signal:"):
                try:
                    value = _recovery_json(body.removeprefix("native:signal:"))
                except (ValueError, TypeError):
                    result["invalid_markers"].append(line_number)
                    continue
                event.update(event="native-signal", payload=value)
            else:
                result["invalid_markers"].append(line_number)
                continue
            result["events"].append(event)
            _require(
                len(result["events"]) <= MAX_ENTRIES, "too many full-suite markers"
            )
        after = os.fstat(stream.fileno())
    _require(
        _signature(before) == _signature(after) == _signature(path.lstat()),
        "runtime log changed during indexing",
    )
    result["continuous10"] = _continuous_index(
        path, relative, stop_line=base_end, full_suite=True
    )
    result["recovery_ten"] = _recovery_index(
        path, relative, stop_line=recovery_end, full_suite=True
    )
    result["sub_status"] = {
        key: _sub_status(result[key], scenario)
        for key, scenario in (
            ("continuous10", "continuous10"),
            ("recovery_ten", "recovery-ten"),
        )
    }
    return result


def _full_boot(boot: dict[str, Any], run_id: str, platform: str) -> None:
    _require(
        set(boot) == {"run_id", "source", "pid", "boot_id", "bundle_id", "executable"}
        and boot["run_id"] == run_id
        and boot["source"] == "launch"
        and type(boot["pid"]) is int
        and boot["pid"] > 0
        and isinstance(boot["boot_id"], str)
        and str(uuid.UUID(boot["boot_id"])) == boot["boot_id"]
        and boot["bundle_id"]
        == (ANDROID_PACKAGE if platform == "android" else BUNDLE_ID)
        and isinstance(boot["executable"], str)
        and boot["executable"].startswith("file:"),
        "full-suite native boot identity drift",
    )


def _passed_full_structure(document: dict[str, Any]) -> None:
    index = document["flow_index"]
    _require(
        index["scenario"] == "full-suite" and not index["error_markers"],
        "full-suite scenario/error markers",
    )
    for key, scenario in (
        ("continuous10", "continuous10"),
        ("recovery_ten", "recovery-ten"),
    ):
        _passed_structure(_sub_document(index[key], scenario))
    _require(
        index["sub_status"] == {"continuous10": "passed", "recovery_ten": "passed"},
        "full-suite incomplete sub-index",
    )
    events = index["events"]
    _require(
        [row["event"] for row in events]
        == [
            "runtime-start",
            "recovery-awaiting",
            "native-signal",
            "recovery-ready",
            "runtime-close",
            "passed",
        ],
        "full-suite requires exact total runtime/handoff/final markers",
    )
    start, awaiting, signal, ready, close, passed = events
    run_id, nonce = start["run_id"], start["nonce"]
    _require(str(uuid.UUID(run_id)) == run_id, "full-suite canonical run UUID")
    _require(
        all(row.get("run_id", run_id) == run_id for row in events)
        and close["nonce"] == nonce,
        "full-suite runId/nonce drift",
    )
    base, recovery = index["continuous10"], index["recovery_ten"]
    for sub, field in ((base, "session"), (recovery, "run_id")):
        _require(
            all(
                row[field] == run_id and row["nonce"] == nonce
                for row in sub["runtime_markers"]
            ),
            "full-suite shared runtime identity/nonce drift",
        )
    lines = [
        start["line"],
        base["runtime_markers"][0]["line"],
        base["runtime_markers"][1]["line"],
        base["summaries"][0]["line"],
        awaiting["line"],
        signal["line"],
        ready["line"],
        recovery["runtime_markers"][0]["line"],
        recovery["runtime_markers"][1]["line"],
        recovery["summaries"][0]["line"],
        close["line"],
        passed["line"],
    ]
    _require(all(a < b for a, b in pairwise(lines)), "full-suite phase order invalid")
    for key, lower, upper in (
        ("continuous10", lines[1], lines[3]),
        ("recovery_ten", lines[7], lines[9]),
    ):
        _require(
            all(lower <= line <= upper for line in index["phase_lines"][key]),
            "full-suite markers outside their phase",
        )
        _require(
            index[key]["flows"][-1]["end_line"] == upper,
            "full-suite last segment crossed phase boundary",
        )
    boots = index["native_bootstraps"]
    _require(
        [row["kind"] for row in boots].count("rf-recovery-ten:native:bootstrap:") == 1
        and [row["kind"] for row in boots].count("rf-host:bootstrap:") == 1,
        "full-suite requires unique launch bootstraps",
    )
    boot = next(
        row["payload"] for row in boots if row["kind"].startswith("rf-recovery-ten:")
    )
    _full_boot(boot, run_id, document["platform"])
    for row in boots:
        _require(
            row["line"] < start["line"]
            and all(
                _canonical(row["payload"].get(k)) == _canonical(boot[k])
                for k in ("pid", "boot_id", "bundle_id", "executable")
            ),
            "full-suite control/recovery native identity drift",
        )
    _require(
        _canonical(signal["payload"])
        == _canonical(
            {
                **boot,
                "source": "payload-url",
                "url": f"blehub-rn-spike://recover/{run_id}/begin",
            }
        ),
        "full-suite native begin identity drift",
    )
    _require(
        all(document["references"].values()), "missing required evidence references"
    )


def _full_proofs(attempt: Path, document: dict[str, Any]) -> None:
    """Bind host-authored proof schemas/hashes, without replaying live RF checks.

    Recorded absolute paths are labels only: relocated bundles never open them.
    Physical counters, SDK ACK deadlines and receipt authority stay with host.
    """
    refs = document["references"]
    index = document["flow_index"]
    start = index["events"][0]
    run_id, nonce = start["run_id"], start["nonce"]
    boot = index["events"][2]["payload"]
    boot = {**boot, "source": "launch"}
    boot.pop("url")

    def load(role: str) -> dict[str, Any]:
        return _json(attempt / refs[role]["path"])

    def same(actual: Any, expected: Any, label: str) -> None:
        _require(
            _canonical(actual) == _canonical(expected), f"full-suite {label} drift"
        )

    def binding(value: dict[str, Any], role: str) -> None:
        same(value["sha256"], refs[role]["sha256"], f"{role} hash binding")
        same(
            Path(value["path"]).name,
            PurePosixPath(refs[role]["path"]).name,
            f"{role} path binding",
        )

    identity, capture = load("runtime_identity"), load("runtime_capture")
    full, base, recovery = (
        load(role)
        for role in ("ledger_full_suite", "ledger_continuous10", "ledger_recovery_ten")
    )
    for value in (identity, capture, full):
        for key, expected in (
            ("scenario", "full-suite"),
            ("status", "passed"),
            ("platform", document["platform"]),
            ("run_id", run_id),
        ):
            same(value.get(key), expected, key)
    for value in (identity, capture):
        same(value.get("device"), document["device"], "capture device")
    same(identity.get("bootstrap"), boot, "capture native bootstrap")
    same(identity.get("pid"), boot["pid"], "capture PID")
    same(identity.get("executable"), boot["executable"], "capture executable")
    if document["platform"] == "ios":
        # Official SDK JSON is inspected locally, never queried again offline.
        same(
            Path(identity["install_json"]).name,
            "device-install.json",
            "install JSON label",
        )
        installed = load("device_install")
        info, result = installed.get("info"), installed.get("result")
        _require(
            isinstance(info, dict) and isinstance(result, dict),
            "full-suite incomplete official iOS installation",
        )
        same(info.get("outcome"), "success", "official install outcome")
        same(
            info.get("commandType"),
            "devicectl.device.install.app",
            "official install command",
        )
        arguments = info.get("arguments")
        _require(
            isinstance(arguments, list)
            and arguments[:4] == ["devicectl", "device", "install", "app"]
            and arguments.count("--device") == 1
            and arguments[arguments.index("--device") + 1 :][:1]
            == [document["device"]],
            "full-suite foreign official install device",
        )
        same(
            result.get("deviceIdentifier"),
            identity["device_identifier"],
            "official device identity",
        )
        applications = result.get("installedApplications")
        _require(
            isinstance(applications, list)
            and all(isinstance(row, dict) for row in applications),
            "full-suite official installed applications",
        )
        matches = [row for row in applications if row.get("bundleID") == BUNDLE_ID]
        _require(len(matches) == 1, "full-suite unique official installed host")
        same(
            matches[0].get("installationURL", "") + SCHEME,
            boot["executable"],
            "official installed executable",
        )
    control_boot = next(
        row["payload"]
        for row in index["native_bootstraps"]
        if row["kind"] == "rf-host:bootstrap:"
    )
    same(control_boot.get("session"), run_id, "control session")
    same(capture.get("control_bootstrap"), control_boot, "capture control bootstrap")
    for key in ("native_acknowledged", "js_acknowledged"):
        same(identity.get(key), list(range(10)), key)
    same(capture.get("host_cycle_actions"), list(range(10)), "host cycle actions")
    same(capture.get("base_status"), "passed", "capture base status")
    for value in (full, base, recovery):
        same(value.get("schema_version"), 1, "ledger schema")
        same(value.get("status"), "passed", "sub ledger status")
        same(value.get("run_id"), run_id, "ledger run identity")
        same(value.get("runtime_nonce"), nonce, "ledger shared nonce")
        same(value.get("native_bootstrap"), boot, "ledger native bootstrap")
    same(capture.get("runtime_nonce"), nonce, "capture nonce")
    same(full.get("session_id"), run_id, "fixture session")
    same(base.get("session_id"), run_id, "base session")
    same(recovery.get("platform"), document["platform"], "recovery platform")
    same(base.get("flows"), list(MODES), "base ledger flows")
    for key, expected in {
        "app_runtimes": 1,
        "hub_probe_lifecycles": 10,
        "resource_snapshots": 50,
        "closed_zero_snapshots": 10,
    }.items():
        same(recovery["counts"].get(key), expected, f"recovery count {key}")
    same(
        recovery.get("closed_resource_totals"),
        dict.fromkeys(RECOVERY_RESOURCE_FIELDS, 0),
        "recovery closed totals",
    )
    same(
        full["counts"].get("recovery_terminals"),
        recovery["counts"].get("terminals"),
        "recovery terminal count",
    )
    for key, expected in {
        "app_runtimes": 1,
        "capture_children": 1,
        "base_flows": 10,
        "recovery_cycles": 10,
    }.items():
        same(full["counts"].get(key), expected, f"ledger count {key}")
    for key, role in (
        ("continuous10", "ledger_continuous10"),
        ("recovery_ten", "ledger_recovery_ten"),
    ):
        binding(full["ledgers"][key], role)
    for key, role in (
        ("handoff", "full_suite_handoff"),
        ("full_suite", "physical_full_suite"),
        ("recovery", "physical_recovery_ten"),
    ):
        binding(full["physical"][key], role)
    for key, role in (
        ("identity", "runtime_identity"),
        ("receipt", "runtime_capture"),
        ("raw_log", "raw_runtime"),
    ):
        binding(full["capture"][key], role)
    for value in (base["physical"], full["physical"]["base"]):
        _require(len(value) == 2, "full-suite base physical bindings")
        for row, role in zip(
            value, ("cancellation_physical", "watchdog_physical"), strict=True
        ):
            binding(row, role)
    handoff = load("full_suite_handoff")
    same(full.get("handoff"), handoff, "ledger handoff")
    same(capture.get("handoff"), handoff, "capture handoff")
    same(handoff.get("status"), "ready", "handoff status")
    physical = load("physical_full_suite")
    for value in (handoff, physical, load("physical_recovery_ten")):
        same(value.get("run_id"), run_id, "physical run identity")
    same(physical.get("status"), "passed", "full physical status")
    same(full.get("begin"), capture.get("begin"), "begin action")
    same(full["begin"]["native_signal"], index["events"][2]["payload"], "begin signal")
    receipt, request = load("fixture_receipt"), load("fixture_request")
    same(full.get("live_receipt"), receipt, "fixture receipt")
    same(full.get("fixture"), recovery.get("fixture"), "fixture owner")
    same(request.get("session_id"), run_id, "fixture request session")
    same(receipt.get("session_id"), run_id, "fixture receipt session")
    fixture_identity = {key: full["fixture"][key] for key in ("pid", "pgid")}
    for value in (handoff, physical):
        same(value.get("fixture_identity"), fixture_identity, "physical fixture owner")
    for key in ("pid", "pgid"):
        same(
            receipt.get("fixture_" + key),
            fixture_identity[key],
            "receipt fixture owner",
        )

    log = _read_small(attempt / document["runtime_log"], MAX_LOG_BYTES)
    raw = _read_small(attempt / refs["raw_runtime"]["path"], MAX_LOG_BYTES)
    # The capture's line-text projection uses LF. devicectl may emit CRLF;
    # normalize only that terminal sequence, never headers or message bytes.
    # The unchanged raw file remains separately fingerprinted and referenced.
    source = [
        (i + 1, line[:-2] + b"\n" if line.endswith(b"\r\n") else line)
        for i, line in enumerate(raw.splitlines(keepends=True))
        if RAW_PREFIX.encode() in line
    ]
    _require(
        b"".join(line for _, line in source) == log,
        "full-suite one raw stream lineage drift",
    )
    same(
        full["capture"]["raw_line_numbers"], [i for i, _ in source], "raw line numbers"
    )
    same(
        full["capture"]["recovery_raw_line_numbers"],
        [i for i, line in source if RECOVERY_PREFIX.encode() in line],
        "recovery raw line numbers",
    )
    projection = b"".join(
        line for _, line in source if RECOVERY_PREFIX.encode() in line
    )
    _require(
        projection
        == _read_small(attempt / refs["recovery_runtime"]["path"], MAX_LOG_BYTES),
        "full-suite recovery projection drift",
    )
    base_end = index["continuous10"]["summaries"][0]["line"]
    same(
        base.get("runtime_sha256"),
        hashlib.sha256(b"".join(log.splitlines(keepends=True)[:base_end])).hexdigest(),
        "base raw prefix hash",
    )


def _adapter_proofs(attempt: Path, document: dict[str, Any]) -> None:
    """Offline same-run structure/lineage only, never live RF authorization.

    Check host command/state/time envelopes and the provider's hash-bound GAP
    pair/cleanup schema, without replaying device state, deadlines or liveness.
    """
    from tools.react_native.adapter_transition_ledger import (
        PREFIX,
        verify_go,
        verify_physical,
        verify_readiness,
        verify_runtime,
        verify_state_control,
    )

    refs = document["references"]

    def load(role: str) -> dict[str, Any]:
        return _json(attempt / refs[role]["path"])

    def same(actual: Any, expected: Any, label: str) -> None:
        _require(_canonical(actual) == _canonical(expected), f"adapter {label} drift")

    identity = load("runtime_identity")
    ledger = load("ledger_adapter_transition")
    physical = load("physical_adapter_transition")
    for value in (identity, ledger):
        same(value.get("schema_version"), 1, "schema")
        same(value.get("platform"), "android", "platform")
        same(value.get("scenario"), "adapter-transition", "scenario")
    run_id = identity["run_id"]
    for value in (identity, ledger, physical):
        same(value.get("status"), "passed", "sidecar status")
        same(value.get("run_id"), run_id, "sidecar run identity")
    verify_state_control(load("adapter_state_control"), run_id, document["device"])
    request, receipt = load("fixture_request"), load("fixture_receipt")
    raw = _read_small(attempt / refs["raw_runtime"]["path"], MAX_LOG_BYTES).decode(
        "utf-8"
    )
    text = _read_small(attempt / document["runtime_log"], MAX_LOG_BYTES).decode("utf-8")
    runtime = verify_runtime(
        text, run_id, ledger["service_uuid"], attempt / document["runtime_log"]
    )
    verify_runtime(
        raw, run_id, ledger["service_uuid"], attempt / refs["raw_runtime"]["path"]
    )
    mode = runtime.get("adapter_mode", "startup-off")
    same(document.get("adapter_mode", "startup-off"), mode, "bundle mode")
    same(identity.get("adapter_mode", "startup-off"), mode, "capture mode")
    same(document["flow_index"].get("adapter_mode", "startup-off"), mode, "index mode")
    verify_go(load("adapter_transition_go"), run_id, mode)
    if "adapter_mode" in document:
        same(runtime.get("adapter_mode"), mode, "explicit runtime mode")
        same(identity.get("adapter_mode"), mode, "explicit capture mode")
        same(physical.get("adapter_mode"), mode, "explicit physical mode")
        control_path = "coordination/adapter-transition-control.json"
        _require(
            control_path in {row["path"] for row in document["files"]},
            "missing explicit adapter control",
        )
        same(
            _json(attempt / control_path),
            {"schema_version": 1, "run_id": run_id, "adapter_mode": mode},
            "explicit control mode",
        )
        same(
            load("adapter_transition_go").get("adapter_mode"), mode, "explicit go mode"
        )
    if mode == "active-loss":
        from tests.peripherals.nrf52840.adapter_transition import (
            ARM_NAME,
            CONTROL_NAME,
            LOSS_NAME,
            OFF_CONFIRMED,
            OFF_READY,
            OFF_REQUEST,
        )

        inventory = {row["path"]: row for row in document["files"]}
        protocol = physical.get("active_protocol", {})
        for name in (
            CONTROL_NAME,
            ARM_NAME,
            LOSS_NAME,
            OFF_REQUEST,
            OFF_READY,
            OFF_CONFIRMED,
        ):
            relative = "coordination/" + name
            _require(relative in inventory, f"missing active mode/handshake raw {name}")
            value = _json(attempt / relative)
            if name == CONTROL_NAME:
                same(
                    value,
                    {"schema_version": 1, "run_id": run_id, "adapter_mode": mode},
                    "immutable active control",
                )
            else:
                same(protocol.get(name), value, f"physical raw handshake {name}")
        for name, field, target in (
            (OFF_REQUEST, "loss_ready_sha256", LOSS_NAME),
            (OFF_READY, "request_sha256", OFF_REQUEST),
            (OFF_CONFIRMED, "off_ready_sha256", OFF_READY),
        ):
            same(
                protocol[name].get(field),
                inventory["coordination/" + target]["sha256"],
                "active handshake bytes binding",
            )
        actions = identity.get("active_actions")
        _require(
            type(actions) is dict
            and set(actions)
            == {
                "arm_sent_monotonic",
                "off_requested_monotonic",
                "off_started_monotonic",
                "off_finished_monotonic",
                "arm_native_ack",
                "arm_js_ack",
            },
            "active host action schema",
        )
        _require(
            actions["arm_native_ack"] is True and actions["arm_js_ack"] is True,
            "active ARM ACKs",
        )
        times = [
            actions[k]
            for k in (
                "arm_sent_monotonic",
                "off_requested_monotonic",
                "off_started_monotonic",
                "off_finished_monotonic",
            )
        ]
        _require(
            all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in times)
            and times == sorted(times)
            and times[2] - times[1] < 1
            and times[3] - times[2] < 4,
            "active host fault budget/order",
        )
        radio_off = load("adapter_state_control")["actions"][1]
        _require(
            times[2]
            <= radio_off["started_monotonic"]
            <= radio_off["confirmed_monotonic"]
            <= times[3],
            "active OFF journal/action correlation",
        )
    same(
        physical.get("go_sha256"),
        refs["adapter_transition_go"]["sha256"],
        "physical go bytes hash",
    )
    readiness = load("adapter_transition_ready")
    verify_readiness(
        readiness, receipt_sha256=refs["fixture_receipt"]["sha256"], runtime=runtime
    )
    same(physical.get("readiness"), readiness, "physical readiness sidecar")
    verify_physical(
        physical,
        request=request,
        receipt=receipt,
        receipt_sha256=refs["fixture_receipt"]["sha256"],
        runtime=runtime,
    )

    def projection(content: str) -> str:
        return "".join(
            line[line.index(PREFIX) :].removesuffix("\n").removesuffix("\r") + "\n"
            for line in content.splitlines(keepends=True)
            if PREFIX in line
        )

    # Projection may retain threadtime headers or strip them, and normalize CRLF;
    # marker bytes/order must remain identical to the one raw capture.
    same(projection(text), projection(raw), "raw runtime lineage")
    for key, value in runtime.items():
        same(ledger.get(key), value, f"runtime ledger {key}")
    for key in ("pid", "boot_id", "native_bootstrap"):
        same(identity.get(key), runtime[key], f"runtime identity {key}")
    markers = document["flow_index"]["runtime_markers"]
    same([row["run_id"] for row in markers], [run_id, run_id], "index run identity")
    same(
        [row["nonce"] for row in markers], [runtime["runtime_nonce"]] * 2, "index nonce"
    )


def _ios_pending_proofs(attempt: Path, document: dict[str, Any]) -> None:
    """Bind iOS owner JSON, both projections and official SDK receipts offline.

    No live receipt/liveness reauthorization: the parent ledger owns RF semantics.
    Android's historical v1 references and structural-only checks stay unchanged.
    """
    from tools.react_native import ios_pending_reload_capture as capture_api
    from tools.react_native import pending_reload_identity, pending_reload_ledger
    from tools.react_native.host_contract import APP_NAME, BUNDLE_ID, SCHEME
    from tools.react_native.ios_sdk import official_result

    refs = document["references"]

    def path(role: str) -> Path:
        _require(isinstance(refs.get(role), dict), f"missing iOS pending {role}")
        return attempt / refs[role]["path"]

    def same(actual: Any, expected: Any, label: str) -> None:
        _require(
            _canonical(actual) == _canonical(expected), f"iOS pending {label} drift"
        )

    log = _read_small(attempt / document["runtime_log"], MAX_LOG_BYTES).decode("utf-8")
    raw = _read_small(path("raw_runtime"), MAX_LOG_BYTES).decode("utf-8")
    normalized = []
    for line in raw.splitlines(keepends=True):
        if capture_api.RAW_PREFIX not in line:
            continue
        _require(
            line.endswith("\n") and line.count(capture_api.RAW_PREFIX) == 1,
            "truncated/ambiguous iOS pending raw marker",
        )
        normalized.append(
            line[line.index(capture_api.RAW_PREFIX) :].rstrip("\r\n") + "\n"
        )
    same("".join(normalized), log, "raw stream lineage")
    same(
        _read_small(path("pending_projection"), MAX_LOG_BYTES).decode("utf-8"),
        capture_api.projection(log, PENDING_PREFIX),
        "physical pending projection",
    )
    same(
        _read_small(path("native_identity_log"), MAX_LOG_BYTES).decode("utf-8"),
        capture_api.projection(log, pending_reload_identity.HOST_PREFIX),
        "native identity projection",
    )
    identity, capture = _json(path("runtime_identity")), _json(path("runtime_capture"))
    ledger = _json(path("ledger_pending_reload"))
    run_id = document["flow_index"]["runtime_markers"][0]["run_id"]
    try:
        native = pending_reload_identity.verify(log, run_id)
        runtime = pending_reload_ledger._runtime(
            capture_api.projection(log, PENDING_PREFIX),
            run_id,
            ledger["service_uuid"],
            platform="ios",
        )
    except (RuntimeError, ValueError, TypeError, KeyError) as error:
        raise EvidenceError(f"iOS pending native/runtime proof: {error}") from error
    for value in (identity, capture, ledger):
        same(value.get("status"), "passed", "proof status")
        same(value.get("run_id"), run_id, "proof run")
        if value is not ledger:
            same(
                value.get("projection_sha256"),
                capture_api.hashes(log),
                "projection hashes",
            )
    for key in ("pid", "boot_id", "owners", "records"):
        same(identity.get(key), native[key], f"native {key}")
    same(ledger.get("native_identity"), native, "parsed native owner ledger")
    for key, value in runtime.items():
        same(ledger.get(key), value, f"runtime ledger {key}")
    same(
        capture.get("host_actions"),
        ["arm-1", "reload", "arm-2"],
        "host action schedule",
    )
    same(ledger.get("host_actions"), capture["host_actions"], "ledger actions")
    same(
        document["artifact"]["fingerprint"].get("executable"),
        SCHEME,
        "candidate executable",
    )
    command = capture.get("command")
    _require(
        isinstance(command, list) and all(type(arg) is str for arg in command),
        "missing iOS pending console command",
    )
    original_log = Path(identity["install_json"]).parent / document["runtime_log"]
    head = [
        "xcrun",
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        document["device"],
        "--terminate-existing",
        "--activate",
        "--console",
        "--json-output",
        str(original_log.with_suffix(".launch.json")),
        BUNDLE_ID,
    ]
    _require(command[: len(head)] == head, "iOS pending console launch identity drift")
    options = command[len(head) :]
    expected_options = [
        "--blehub-nitro-rf-pending-read-reload-service",
        ledger["service_uuid"],
        "--blehub-nitro-rf-pending-read-reload-notification",
        f"dev.blehub.rn.reload.{run_id}",
        "--blehub-nitro-rf-pending-read-reload-run-id",
        run_id,
        "--blehub-nitro-rf-expected-local-name",
    ]
    _require(
        options[:-1] == expected_options and options[-1].strip() != "",
        "iOS pending console launch arguments drift",
    )
    _require(
        type(capture.get("capture_pid")) is int
        and capture["capture_pid"] > 0
        and capture.get("console_cleanup") == "detached",
        "missing owned console detachment",
    )

    def official(file: Path, command_type: str) -> dict[str, Any]:
        try:
            return official_result(file, command_type, document["device"])
        except (RuntimeError, ValueError, TypeError, KeyError) as error:
            raise EvidenceError(f"iOS pending official JSON: {error}") from error

    installed = official(path("device_install"), "devicectl.device.install.app")
    same(
        installed["deviceIdentifier"],
        identity.get("device_identifier"),
        "official device",
    )
    rows = installed.get("installedApplications")
    _require(
        isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
        "invalid iOS pending installedApplications",
    )
    matches = [row for row in rows if row.get("bundleID") == BUNDLE_ID]
    _require(len(matches) == 1, "nonunique iOS pending installed host")
    url = matches[0].get("installationURL")
    _require(
        isinstance(url, str)
        and url.startswith("file:///")
        and url.endswith(f"/{APP_NAME}/"),
        "invalid iOS pending installation URL",
    )
    same(identity.get("executable"), url + SCHEME, "installed executable")
    arguments = _json(path("device_install"))["info"]["arguments"]
    # The bundle may have been moved for offline audit: compare the originally
    # recorded installation path, not the current attempt's absolute location.
    _require(
        arguments[-1:]
        == [str(Path(identity["install_json"]).parent / document["artifact"]["path"])],
        "official install was not the frozen candidate",
    )
    log_path = attempt / document["runtime_log"]
    actions = capture.get("actions")
    _require(
        isinstance(actions, list) and len(actions) == 3, "missing iOS pending controls"
    )
    for action, expected in zip(actions, ("arm-1", "reload", "arm-2"), strict=True):
        same(action.get("action"), expected, "control action")
        same(action.get("status"), "confirmed", "control result")
        url = (
            f"blehub-rn-spike://{'reload' if expected == 'reload' else 'arm'}/{run_id}"
        )
        same(action.get("url"), url, "control URL")
        receipt = log_path.with_suffix(f".{expected}-payload.json")
        result = official(receipt, "devicectl.device.process.launch")
        same(
            result.get("deviceIdentifier"),
            identity["device_identifier"],
            "payload device",
        )
        process = result.get("process", {})
        same(process.get("processIdentifier"), native["pid"], "payload PID")
        same(process.get("executable"), identity["executable"], "payload executable")
        args = _json(receipt)["info"]["arguments"]
        _require(
            args.count("--payload-url") == 1
            and args[args.index("--payload-url") + 1 :][:1] == [url]
            and args[-1:] == [BUNDLE_ID]
            and not any(flag in args for flag in ("--terminate-existing", "--console")),
            "foreign iOS pending payload arguments",
        )
        for boundary in (expected + "-before", expected + "-after"):
            result = official(
                log_path.with_suffix(f".{boundary}-processes.json"),
                "devicectl.device.info.processes",
            )
            same(
                result.get("deviceIdentifier"),
                identity["device_identifier"],
                "process device",
            )
            rows = result.get("runningProcesses")
            _require(
                isinstance(rows, list)
                and all(
                    isinstance(row, dict)
                    and type(row.get("processIdentifier")) is int
                    and row["processIdentifier"] > 0
                    for row in rows
                ),
                "malformed iOS pending processes",
            )
            matches = [row for row in rows if row["processIdentifier"] == native["pid"]]
            _require(
                len(matches) == 1
                and matches[0].get("executable") == identity["executable"],
                "iOS pending PID/executable changed",
            )


def _permission_names() -> dict[str, str]:
    """Fixed evidence roles, never filenames supplied by a captured App/result."""
    from tests.peripherals.nrf52840 import permission_transition as wire
    from tools.react_native import permission_transition_capture as capture

    return {
        "runtime_capture": capture.RESULT_NAME,
        "raw_runtime": capture.RAW_NAME,
        "runtime_projection": wire.RUNTIME_NAME,
        "permission_projection": "coordination/" + wire.RUNTIME_NAME,
        "permission_state_control": "permission-state-control.json",
        "radio_state_control": "permission-radio-control.json",
        "os_death_proof": capture.PROOF_NAME,
        "os_exit_info": capture.OS_RAW_NAME,
        "ledger_permission_transition": "permission-transition-ledger.json",
        "setup_replacement": "permission-setup-replacement.json",
        "artifact_install_identity": "artifact-install-identity.json",
        "fixture_request": "coordination/fixture-session/fixture-request.json",
        "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
        "fixture_contract": "coordination/fixture-session/contract.json",
        "fixture_profile": "coordination/fixture-session/profile.json",
        "permission_control": "coordination/" + wire.CONTROL_NAME,
        "physical_permission_transition": "coordination/" + wire.PHYSICAL_NAME,
        **{
            f"phase_identity_{phase}": f"permission-transition-{phase}.identity.json"
            for phase in wire.PHASES
        },
        **{
            name.removesuffix(".json").replace("-", "_"): "coordination/" + name
            for name in wire.HANDSHAKE_NAMES
        },
    }


def _permission_index(path: Path, relative: str) -> dict[str, Any]:
    """Raw source ranges plus captured phase/host-boundary metadata, never a pass.

    Every protocol record is indexed, including late/foreign records. A semantic
    pass is checked separately by _permission_proofs and the shared ledger.
    """
    from tools.react_native import permission_transition_ledger as ledger

    root = path.parents[len(PurePosixPath(_safe_name(relative)).parts) - 1]
    names = _permission_names()
    source = root / names["raw_runtime"]
    if source.exists() or source.is_symlink():
        path, relative = source, names["raw_runtime"]
    raw = _read_small(path, PERMISSION_FILE_BYTES)
    try:
        index = ledger.index_runtime(raw.decode("utf-8"), relative)
    except (RuntimeError, UnicodeError) as error:
        index = ledger.index_runtime("", relative)
        index["capture_error"] = str(error)
        return index
    result_path = root / names["runtime_capture"]
    if not result_path.exists() and not result_path.is_symlink():
        return index
    try:
        result = _recovery_json(
            _read_small(result_path, PERMISSION_FILE_BYTES).decode("utf-8")
        )
        phases = result.get("phases", {})
        _require(
            type(phases) is dict and type(result.get("boundaries")) is list,
            "permission capture index metadata",
        )
        for row in index["flows"]:
            row["capture_phase"] = phases.get(row["phase"])
        index["host_boundaries"] = []
        for boundary in result["boundaries"]:
            count = boundary.get("after_marker")
            _require(
                type(count) is int and 0 <= count <= len(index["events"]),
                "permission boundary outside raw marker range",
            )
            index["host_boundaries"].append(
                {
                    **boundary,
                    "raw_after_line": index["events"][count - 1]["line"]
                    if count
                    else 0,
                }
            )
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError) as error:
        index["capture_error"] = str(error)
    return index


def _permission_proofs(attempt: Path, document: dict[str, Any]) -> None:
    """Replay persisted raw/projection, OS and fixture facts through the shared ledger.

    Captured ``passed`` fields are prerequisites, never substitutes for replay.
    Only fixed runner-owned paths are read; no live receipt/device owner is called.
    """
    from tests.peripherals.nrf52840 import permission_transition as wire
    from tools.react_native import permission_transition_ledger as ledger
    from tools.react_native.host_contract import ANDROID_ACTIVITY

    refs, names = document["references"], _permission_names()
    _require(set(refs) == set(names), "permission fixed reference roles")
    _require(
        document["runtime_log"] == names["runtime_projection"],
        "permission canonical runtime path",
    )
    expected = {name for name in names.values() if name.startswith("coordination/")}
    actual = {
        row["path"]
        for row in document["files"]
        if row["path"].startswith("coordination/")
    }
    _require(actual == expected, "permission unexpected/missing coordination files")
    raw: dict[str, bytes] = {}
    remaining = PERMISSION_PROOF_BYTES
    for role, name in names.items():
        ref = refs[role]
        _require(
            type(ref) is dict and _safe_name(ref["path"]) == name,
            f"permission unsafe/unexpected {role} reference",
        )
        path = attempt / name
        _require(
            path.absolute().is_relative_to(attempt.absolute()),
            "permission proof escaped attempt",
        )
        _no_links(path)
        _require(
            path.lstat().st_nlink == 1,
            "permission proof must have one immutable file identity",
        )
        content = _read_small(path, min(PERMISSION_FILE_BYTES, remaining))
        remaining -= len(content)
        _require(
            ref["size"] == len(content)
            and ref["sha256"] == hashlib.sha256(content).hexdigest(),
            f"permission {role} raw hash binding",
        )
        raw[role] = content

    def load(role: str) -> dict[str, Any]:
        return _recovery_json(raw[role].decode("utf-8"))

    def same(actual: Any, expected: Any, label: str) -> None:
        _require(
            _canonical(actual) == _canonical(expected), f"permission {label} drift"
        )

    try:
        result = load("runtime_capture")
        for key, value in {
            "schema_version": 1,
            "platform": "android",
            "scenario": wire.MODE,
            "device": document["device"],
            "status": "passed",
            "primary_error": None,
        }.items():
            same(result.get(key), value, f"capture {key}")
        for key, role in {
            "raw_log": "raw_runtime",
            "runtime_log": "runtime_projection",
            "coordination_runtime_log": "permission_projection",
            "os_journal": "permission_state_control",
            "os_death_proof": "os_death_proof",
            "os_raw_proof": "os_exit_info",
        }.items():
            same(result.get(key), names[role], f"capture {key} path")
        capture_command = result["capture_command"]
        _require(
            type(capture_command) is list
            and capture_command[1:]
            == ["-s", document["device"], "logcat", "-b", "main", "-v", "threadtime"]
            and type(result["capture_pid"]) is int
            and result["capture_pid"] > 0,
            "permission one owned logcat command",
        )
        run_id = result["run_id"]
        same(
            load("permission_control"),
            {"schema_version": 1, "run_id": run_id},
            "immutable control",
        )
        request, receipt = load("fixture_request"), load("fixture_receipt")
        same(receipt.get("port"), request["port"], "fixture request/receipt port")
        for role, key in (
            ("fixture_contract", "contract_sha256"),
            ("fixture_profile", "profile_sha256"),
        ):
            same(request[key], refs[role]["sha256"], f"fixture {role} original bytes")
            _recovery_json(raw[role].decode("utf-8"))
        profile = load("fixture_profile")
        name = profile["advertising"]["local_name"]
        _require(
            type(name) is str and 0 < len(name) <= 512, "permission frozen local name"
        )
        documents = {
            filename: raw[filename.removesuffix(".json").replace("-", "_")]
            for filename in wire.HANDSHAKE_NAMES
        }
        text = raw["runtime_projection"].decode("utf-8")
        source = raw["raw_runtime"].decode("utf-8")
        same(
            "".join(line + "\n" for line in ledger.markers(source)),
            text,
            "all raw records/projection lineage",
        )
        _require(
            raw["permission_projection"] == raw["runtime_projection"],
            "permission provider original marker projection drift",
        )
        options = {
            "capture_result": result,
            "documents": documents,
            "receipt_sha256": refs["fixture_receipt"]["sha256"],
            "death_proof": raw["os_death_proof"],
            "os_raw": raw["os_exit_info"],
            "local_name": name,
        }
        runtime = ledger.verify_runtime(
            text,
            run_id,
            request["service_uuid"],
            names["runtime_projection"],
            **options,
        )
        ledger.verify_runtime(
            source, run_id, request["service_uuid"], names["raw_runtime"], **options
        )
        same(load("ledger_permission_transition"), runtime, "replayed ledger")
        ledger.verify_physical(
            load("physical_permission_transition"),
            request=request,
            receipt=receipt,
            receipt_sha256=refs["fixture_receipt"]["sha256"],
            runtime=runtime,
            documents=documents,
        )
        journal, proof = load("permission_state_control"), load("os_death_proof")
        ledger.verify_permission_control(journal, run_id, document["device"])
        ledger.verify_state_control(
            load("radio_state_control"), run_id, document["device"]
        )
        # The reason8 gate must still be the exact prefix of the final controller
        # journal; cleanup/grant cannot replace or reinterpret the old OS record.
        _require(
            len(proof["actions"]) == 5,
            "permission original death journal prefix length",
        )
        same(proof["actions"], journal["actions"][:5], "original death journal/actions")
        for key in (
            "schema_version",
            "platform",
            "device",
            "run_id",
            "package",
            "user_id",
            "uid",
            "initial_grants",
            "baseline_exit_records",
            "api",
        ):
            same(proof[key], journal[key], f"original death journal {key}")
        for phase in wire.PHASES:
            entry = result["phases"][phase]
            native = runtime["native_bootstraps"][phase]
            same(
                entry["native_bootstrap"], native, f"{phase} original native bootstrap"
            )
            for field, native_field in (
                ("pid", "pid"),
                ("uid", "uid"),
                ("user_id", "user_id"),
                ("native_started_unix_ms", "started_unix_ms"),
            ):
                same(
                    entry["process_identity"][field],
                    native[native_field],
                    f"{phase} original OS/native {field}",
                )
            role = "phase_identity_" + phase
            same(entry["pid_receipt"], names[role], f"{phase} PID receipt path")
            same(entry["pid"], native["pid"], f"{phase} source PID")
            pid_receipt = load(role)
            for key, value in {
                "schema_version": 1,
                "device_serial": document["device"],
                "package_name": ANDROID_PACKAGE,
                "status": "pinned",
                "pid": str(native["pid"]),
            }.items():
                same(pid_receipt.get(key), value, f"{phase} PID receipt {key}")
            budget, elapsed = (
                ledger.moment(pid_receipt["boot_budget_seconds"]),
                ledger.moment(pid_receipt["boot_elapsed_seconds"]),
            )
            _require(0 <= elapsed < budget <= 4, "permission first-PID receipt budget")
            observations = pid_receipt["observations"]
            _require(
                type(observations) is list and 0 < len(observations) <= 64,
                "permission first-PID receipt observations",
            )
            previous = 0.0
            for i, observation in enumerate(observations):
                moment = ledger.moment(observation["elapsed_seconds"])
                _require(
                    previous <= moment <= elapsed,
                    "permission PID observation chronology",
                )
                same(
                    [observation["exit_code"], observation["output"]],
                    [0, str(native["pid"])] if i == len(observations) - 1 else [1, ""],
                    f"{phase} initial PID selection",
                )
                previous = moment
            same(previous, elapsed, f"{phase} PID observation elapsed")
            same(
                entry["closed_resources"],
                None if phase == "active" else runtime["resources"][phase],
                f"{phase} own close resources",
            )
            same(entry["dead"], phase == "active", f"{phase} dead instance")
            same(entry["retired"], phase == "denied", f"{phase} host retire")
            receipts = entry["process_receipts"]
            _require(
                type(receipts) is list and 0 < len(receipts) <= 4096,
                "permission phase process receipts",
            )
            for row in receipts:
                same(
                    row["identity"],
                    entry["process_identity"],
                    f"{phase} native/procstart receipt",
                )
                ledger.moment(row["monotonic"])
        install = load("artifact_install_identity")
        for key, value in {
            "schema_version": 1,
            "status": "passed",
            "apk": document["artifact"]["path"],
            "method": "pinned-sdk-aapt2-dump-badging",
            "package_names": [ANDROID_PACKAGE],
            "launchable_activities": [ANDROID_ACTIVITY],
            "expected_package": ANDROID_PACKAGE,
            "expected_activity": ANDROID_ACTIVITY,
        }.items():
            same(install.get(key), value, f"frozen APK manifest {key}")
        replacement = load("setup_replacement")
        for key, value in {
            "schema_version": 1,
            "run_id": run_id,
            "package": ANDROID_PACKAGE,
            "method": "replace-old-known-host-under-phone-lease",
            "status": "completed",
        }.items():
            same(replacement.get(key), value, f"setup replacement {key}")
    except (
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        UnicodeError,
    ) as error:
        raise EvidenceError(f"permission proof: {error}") from error


def _write_reject_names(platform: str) -> dict[str, str]:
    from tests.peripherals.nrf52840 import write_reject as wire

    return _reject_names(platform, "write-reject", wire)


def _ccc_enable_reject_names(platform: str) -> dict[str, str]:
    from tests.peripherals.nrf52840 import ccc_enable_reject as wire

    return _ccc_reject_names(platform, wire)


def _ccc_disable_reject_names(platform: str) -> dict[str, str]:
    from tests.peripherals.nrf52840 import ccc_disable_reject as wire

    return _ccc_reject_names(platform, wire)


def _ccc_reject_names(platform: str, wire: Any) -> dict[str, str]:
    names = _reject_names(platform, wire.SCENARIO, wire)
    for index, phase in enumerate(wire.PHASES):
        names[f"sdk_output_{phase.replace('-', '_')}"] = (
            f"{wire.SCENARIO}-runtime.control-{index}-sdk-output.log"
        )
        if platform == "ios":
            names[f"sdk_receipt_{phase.replace('-', '_')}"] = (
                f"{wire.SCENARIO}-runtime.control-{index}-notification.json"
            )
    return names


def _reject_names(platform: str, scenario: str, wire: Any) -> dict[str, str]:
    from tests.peripherals.nrf52840 import fixture_session as session

    role = scenario.replace("-", "_")
    names = {
        "runtime_projection": f"{scenario}-runtime.log",
        "raw_runtime": f"{scenario}-runtime.raw.log",
        "runtime_identity": f"{scenario}-runtime.identity.json",
        "runtime_capture": f"{scenario}-runtime.capture.json",
        "fixture_request": "coordination/fixture-session/fixture-request.json",
        "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
        "fixture_contract": f"coordination/fixture-session/{session.CONTRACT_NAME}",
        "fixture_profile": f"coordination/fixture-session/{session.PROFILE_NAME}",
        role + "_control": f"coordination/{wire.CONTROL_NAME}",
        "physical_" + role: f"coordination/{wire.PHYSICAL_NAME}",
        "ledger_" + role: f"{scenario}-ledger.json",
        "physical_runtime": "coordination/runtime.log",
    }
    names.update(
        ("request_" + request.replace("-", "_"), f"coordination/request-{request}.json")
        for request in wire.REQUESTS
    )
    names.update(
        ("signal_" + phase.replace("-", "_"), f"coordination/signal-{phase}.json")
        for phase in wire.PHASES
    )
    if platform == "android":
        names["artifact_install_identity"] = "artifact-install-identity.json"
    else:
        names["device_install"] = "device-install.json"
    return names


def _write_reject_proofs(attempt: Path, document: dict[str, Any]) -> None:
    from tests.peripherals.nrf52840 import write_reject as wire
    from tools.react_native import write_reject_ledger

    _reject_proofs(attempt, document, wire, write_reject_ledger.verify)


def _ccc_enable_reject_proofs(attempt: Path, document: dict[str, Any]) -> None:
    from tests.peripherals.nrf52840 import ccc_enable_reject as wire
    from tools.react_native import ccc_enable_reject_ledger

    _reject_proofs(attempt, document, wire, ccc_enable_reject_ledger.verify)


def _notification_observation_proofs(
    attempt: Path, document: dict[str, Any], *, complete: bool
) -> None:
    from tests.peripherals.nrf52840 import (
        notification_observation_capture as observation,
    )

    root = attempt / "coordination"
    present = any(
        (root / name).exists()
        for name in (observation.REQUEST_NAME, observation.CAPTURE_NAME)
    )
    if not present:
        return
    _require(
        document["scenario"] == "ccc-disable-reject", "observation outside CCC disable"
    )
    try:
        observation.verify_capture(root, complete=complete)
    except (
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
    ) as error:
        raise EvidenceError(f"notification observation proof: {error}") from error


def _ccc_disable_reject_proofs(attempt: Path, document: dict[str, Any]) -> None:
    from tests.peripherals.nrf52840 import ccc_disable_reject as wire
    from tools.react_native import ccc_disable_reject_ledger

    _reject_proofs(attempt, document, wire, ccc_disable_reject_ledger.verify)


def _reject_proofs(
    attempt: Path, document: dict[str, Any], wire: Any, verify: Any
) -> None:
    """Replay recorded facts only, with fixed local inputs and no device access.

    Stage the original bytes privately so receipt freshness uses the recorded run
    interval, not extraction mtimes. Absolute capture paths are labels, never IO.
    The shared ledger owns tickets, native errors/readbacks and retirement policy.
    """
    from tests.peripherals.nrf52840.cancellation_control import load_control
    from tools.react_native.capture_io import PrefixStream
    from tools.react_native.host_contract import ANDROID_ACTIVITY
    from tools.react_native.ios_sdk import official_result

    scenario = document["scenario"]
    scenario_role = scenario.replace("-", "_")
    marker_prefixes = (f"rf-{scenario}:", "rf-control:", "rf-host:")
    refs = document["references"]
    names = (
        _ccc_reject_names(document["platform"], wire)
        if scenario in ("ccc-enable-reject", "ccc-disable-reject")
        else _write_reject_names(document["platform"])
    )
    if scenario == "ccc-disable-reject" and "notification_observation_request" in refs:
        from tests.peripherals.nrf52840 import (
            notification_observation_capture as observation,
        )

        names.update(
            notification_observation_request=f"coordination/{observation.REQUEST_NAME}",
            notification_observation=f"coordination/{observation.CAPTURE_NAME}",
        )
    _require(set(refs) == set(names), f"{scenario} fixed reference roles")
    _require(
        document["runtime_log"] == names["runtime_projection"],
        f"{scenario} canonical runtime path",
    )
    raw = {}
    for role, name in names.items():
        ref = refs[role]
        _require(ref["path"] == name, f"write-reject {role} path")
        content = _read_small(attempt / name, wire.MAX_RUNTIME_BYTES)
        _require(
            ref["size"] == len(content)
            and ref["sha256"] == hashlib.sha256(content).hexdigest(),
            f"write-reject {role} hash drift",
        )
        raw[role] = content

    def load(role: str) -> dict[str, Any]:
        return _recovery_json(raw[role].decode("utf-8"))

    def same(actual: Any, expected: Any, label: str) -> None:
        _require(
            _canonical(actual) == _canonical(expected), f"write-reject {label} drift"
        )

    def official(path: Path, command_type: str) -> dict[str, Any]:
        try:
            return official_result(path, command_type, document["device"])
        except RuntimeError as error:
            raise EvidenceError(
                f"write-reject official SDK receipt: {error}"
            ) from error

    request, receipt = load("fixture_request"), load("fixture_receipt")
    identity, capture = load("runtime_identity"), load("runtime_capture")
    run_id, service = request["session_id"], request["service_uuid"]
    owner = {key: receipt["fixture_" + key] for key in ("pid", "pgid")}
    _require(
        all(type(value) is int and value > 0 for value in owner.values()),
        f"{scenario} positive fixture PID/PGID",
    )
    for value in (identity, capture):
        for key, expected in {
            "schema_version": 1,
            "scenario": scenario,
            "platform": document["platform"],
            "device": document["device"],
            "run_id": run_id,
            "status": "passed",
        }.items():
            same(value.get(key), expected, key)
        _require(not value.get("primary_error"), f"{scenario} capture primary error")
    same(identity.get("fixture_identity"), owner, "capture fixture owner")
    same(capture.get("cleanup_errors"), [], "capture cleanup")
    _require(not capture.get("publication_error"), f"{scenario} publication failure")
    same(capture.get("pending"), None, "pending control")
    text = raw["runtime_projection"].decode("utf-8")
    _require(
        raw["physical_runtime"] == raw["runtime_projection"],
        f"{scenario} provider runtime snapshot drift",
    )
    started = datetime.fromisoformat(document["started_at"]).timestamp()
    finished = datetime.fromisoformat(document["finished_at"]).timestamp()
    captured = datetime.fromisoformat(receipt["captured_at_utc"]).timestamp()
    _require(started <= captured <= finished, f"{scenario} receipt outside run")
    with tempfile.TemporaryDirectory(prefix=f"blehub-{scenario}-replay-") as temporary:
        root = Path(temporary)
        for role, name in names.items():
            if name.startswith("coordination/"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw[role])
        fixture_request = root / names["fixture_request"]
        os.utime(fixture_request, (started, started))
        replay = verify(
            text,
            platform=document["platform"],
            run_id=run_id,
            service_uuid=service,
            physical_path=root / names["physical_" + scenario_role],
            fixture_request=fixture_request,
            fixture_identity=(owner["pid"], owner["pgid"]),
        )
        same(load("ledger_" + scenario_role), replay, "replayed ledger")
        control = load_control(root / names[scenario_role + "_control"])
        control_document = load(scenario_role + "_control")
        for value in (identity, capture):
            same(value.get("control"), control_document, "capture control")
        boot = identity["bootstrap"]
        pid = boot["pid"]
        _require(type(pid) is int and pid > 0, f"{scenario} native PID")
        _require(
            str(uuid.UUID(boot["boot_id"])) == boot["boot_id"],
            f"{scenario} native boot UUID",
        )
        same(
            boot,
            {
                **control_document,
                "pid": pid,
                "boot_id": boot["boot_id"],
                "bundle_id": ANDROID_PACKAGE
                if document["platform"] == "android"
                else BUNDLE_ID,
                "executable": identity["executable"],
            },
            "native bootstrap",
        )
        same(identity.get("pid"), pid, "capture PID")
        same(capture.get("native_bootstrap"), boot, "capture bootstrap")
        raw_path = root / "raw.log"
        raw_path.write_bytes(raw["raw_runtime"])
        stream = PrefixStream(
            raw_path,
            pid if document["platform"] == "android" else None,
            prefix=wire.PREFIX,
        )
        try:
            captured_lines = stream.poll()
        except RuntimeError as error:
            raise EvidenceError(f"write-reject raw framing: {error}") from error
        lines = [
            wire.PREFIX + line.split(wire.PREFIX, 1)[1].strip()
            for line in captured_lines
            if line.split(wire.PREFIX, 1)[1].strip().startswith(marker_prefixes)
        ]
        _require(
            stream.complete == len(stream.previous), f"{scenario} truncated raw stream"
        )
        same("".join(line + "\n" for line in lines), text, "raw/normalized lineage")
        same(capture.get("normalized_lines"), len(lines), "normalized line count")
        bodies = [line.removeprefix(wire.PREFIX) for line in lines]
        native = [body for body in bodies if body.startswith("rf-host:")]
        signals = [{**boot, "phase": phase} for phase in wire.PHASES]
        expected_native = [("bootstrap", boot), *(("signal", s) for s in signals)]
        same(
            [
                (body.split(":", 2)[1], _recovery_json(body.split(":", 2)[2]))
                for body in native
            ],
            expected_native,
            "raw native bootstrap/signals",
        )
        _require(
            bodies[0].startswith("rf-host:bootstrap:"), f"{scenario} bootstrap order"
        )
        for value in (identity, capture):
            same(value.get("native_signals"), signals, "native signal receipts")
            for key in ("native_acknowledged", "js_acknowledged"):
                same(value.get(key), list(wire.PHASES), key)
        actions = capture["actions"]
        same(
            [action["phase"] for action in actions],
            list(wire.PHASES),
            "control actions",
        )
        for index, (action, phase, request_phase) in enumerate(
            zip(actions, wire.PHASES, wire.REQUESTS, strict=True)
        ):
            for key, expected in {
                "control": control_document,
                "status": "relayed-process-stable",
                "url": control.uri(phase),
                "delivery": "android-intent"
                if document["platform"] == "android"
                else "darwin-notification",
                "notification": None
                if document["platform"] == "android"
                else control.notification(phase),
                "sdk_exit": 0,
            }.items():
                same(action.get(key), expected, f"control {phase} {key}")
            moments = [
                action[key] for key in ("requested_at", "native_ack_at", "js_ack_at")
            ]
            _require(
                all(
                    type(t) in (int, float) and math.isfinite(t) and t >= 0
                    for t in moments
                )
                and moments[0] <= moments[1] <= moments[2]
                and moments[2] - moments[0] < 2,
                f"{scenario} original control ACK budget/order",
            )
            signal_line = next(
                i
                for i, body in enumerate(bodies)
                if body.startswith("rf-host:signal:")
                and _recovery_json(body.split(":", 2)[2])["phase"] == phase
            )
            _require(
                bodies.index(control.marker("ready", request_phase))
                < signal_line
                < bodies.index(control.marker("observed", phase)),
                f"{scenario} native/JS ACK order",
            )
            output = f"{scenario}-runtime.control-{index}-sdk-output.log"
            same(action.get("sdk_output"), output, "SDK output path")
            sdk_output = _read_small(attempt / output, wire.MAX_RUNTIME_BYTES).decode(
                "utf-8"
            )
            if scenario in ("ccc-enable-reject", "ccc-disable-reject"):
                _require(
                    "Error:" not in sdk_output and "Exception" not in sdk_output,
                    "ccc-enable-reject SDK rejection",
                )
                same(capture["budgets_seconds"]["ack"], 2.0, "ACK budget")
                if index:
                    _require(
                        actions[index - 1]["js_ack_at"] <= moments[0],
                        "ccc-enable-reject overlapping original controls",
                    )
                command = action.get("sdk_command")
                _require(
                    isinstance(command, list)
                    and all(isinstance(arg, str) for arg in command),
                    "ccc-enable-reject SDK command",
                )
                if document["platform"] == "android":
                    same(
                        command[1:],
                        [
                            "-s",
                            document["device"],
                            "shell",
                            "am",
                            "start",
                            "-n",
                            f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}",
                            "-a",
                            "android.intent.action.VIEW",
                            "-d",
                            control.uri(phase),
                        ],
                        "explicit SDK control command",
                    )
            if document["platform"] == "ios":
                name = f"{scenario}-runtime.control-{index}-notification.json"
                same(action.get("sdk_receipt"), name, "notification receipt path")
                result = official(attempt / name, "devicectl.device.notification.post")
                same(
                    result["deviceIdentifier"],
                    identity["device_identifier"],
                    "notification device",
                )
                args = _json(attempt / name)["info"]["arguments"]
                _require(
                    args.count("--name") == 1
                    and args[args.index("--name") + 1 :][:1]
                    == [control.notification(phase)],
                    f"{scenario} notification name",
                )
                if scenario in ("ccc-enable-reject", "ccc-disable-reject"):
                    same(command[1:], args, "official notification SDK command")

    if document["platform"] == "android":
        install = load("artifact_install_identity")
        for key, expected in {
            "schema_version": 1,
            "status": "passed",
            "apk": document["artifact"]["path"],
            "method": "pinned-sdk-aapt2-dump-badging",
            "package_names": [ANDROID_PACKAGE],
            "launchable_activities": [ANDROID_ACTIVITY],
            "expected_package": ANDROID_PACKAGE,
            "expected_activity": ANDROID_ACTIVITY,
        }.items():
            same(install.get(key), expected, f"APK install identity {key}")
        _require(
            identity["executable"].startswith("file:/")
            and identity["executable"].endswith("/base.apk"),
            f"{scenario} package code path",
        )
    else:
        same(
            Path(identity["install_json"]).name, "device-install.json", "install label"
        )
        result = official(
            attempt / names["device_install"], "devicectl.device.install.app"
        )
        same(
            result["deviceIdentifier"], identity["device_identifier"], "install device"
        )
        applications = [
            row
            for row in result["installedApplications"]
            if row["bundleID"] == BUNDLE_ID
        ]
        _require(len(applications) == 1, f"{scenario} unique installed host")
        same(
            applications[0]["installationURL"] + SCHEME,
            identity["executable"],
            "installed executable",
        )
        if scenario in ("ccc-enable-reject", "ccc-disable-reject"):
            from tools.react_native.host_contract import APP_NAME

            same(
                document["artifact"]["fingerprint"]["executable"],
                SCHEME,
                "candidate executable",
            )
            _require(
                applications[0]["installationURL"].startswith("file:///")
                and applications[0]["installationURL"].endswith(f"/{APP_NAME}/"),
                "ccc-enable-reject installed host URL",
            )
            # Original absolute paths remain labels when a bundle is relocated.
            arguments = load("device_install")["info"]["arguments"]
            same(
                arguments[-1:],
                [
                    str(
                        Path(identity["install_json"]).parent
                        / document["artifact"]["path"]
                    )
                ],
                "official frozen candidate install",
            )


def _references(
    files: list[dict[str, Any]],
    platform: str,
    runtime_log: str,
    scenario: str = "continuous10",
) -> dict[str, Any]:
    _validate_scenario(platform, scenario)
    by_name = {row["path"]: row for row in files}
    if scenario == "ios-adapter-startup":
        from tools.react_native.ios_adapter_startup_evidence import names

        return {role: by_name.get(name) for role, name in names().items()}
    if scenario == "write-reject":
        return {
            role: by_name.get(name)
            for role, name in _write_reject_names(platform).items()
        }
    if scenario == "ccc-enable-reject":
        return {
            role: by_name.get(name)
            for role, name in _ccc_enable_reject_names(platform).items()
        }
    if scenario == "ccc-disable-reject":
        names = _ccc_disable_reject_names(platform)
        from tests.peripherals.nrf52840 import (
            notification_observation_capture as observation,
        )

        if f"coordination/{observation.REQUEST_NAME}" in by_name:
            names.update(
                notification_observation_request=f"coordination/{observation.REQUEST_NAME}",
                notification_observation=f"coordination/{observation.CAPTURE_NAME}",
            )
        return {role: by_name.get(name) for role, name in names.items()}
    if scenario == "permission-transition":
        return {role: by_name.get(name) for role, name in _permission_names().items()}
    if scenario == "adapter-transition":
        required = {
            "runtime_identity": "adapter-transition-runtime.identity.json",
            "fixture_request": "coordination/fixture-session/fixture-request.json",
            "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
            "physical_adapter_transition": "coordination/adapter-transition-physical.json",
            "ledger_adapter_transition": "adapter-transition-ledger.json",
            "adapter_state_control": "adapter-state-control.json",
            "adapter_transition_ready": "coordination/adapter-transition-ready.json",
            "adapter_transition_go": "coordination/go",
            "raw_runtime": "adapter-transition-runtime.raw.log",
            "artifact_install_identity": "artifact-install-identity.json",
        }
        return {role: by_name.get(name) for role, name in required.items()}
    log = PurePosixPath(runtime_log)
    identity_log = log
    ios_pending = platform == "ios" and scenario == "pending-read-reload"
    if (
        platform == "ios"
        and scenario not in ("recovery-ten", "full-suite", "pending-read-reload")
        and str(log.with_suffix(".rf-host-identity.json")) not in by_name
    ):
        identity_log = log.with_name(log.stem + ".attempt.log")
    required = {
        "runtime_identity": str(
            identity_log.with_suffix(
                ".identity.json"
                if platform == "android"
                or scenario in ("recovery-ten", "full-suite")
                or ios_pending
                else ".rf-host-identity.json"
            )
        ),
        "fixture_request": "coordination/fixture-session/fixture-request.json",
        "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
    }
    if scenario == "pending-read-reload":
        required["physical_pending_reload"] = (
            "coordination/pending-reload-physical.json"
        )
    elif scenario == "recovery-ten":
        required["physical_recovery_ten"] = "coordination/recovery-ten-physical.json"
        required["ledger_recovery_ten"] = "recovery-ten-ledger.json"
    else:
        required["cancellation_physical"] = (
            "coordination/flow-8-rf-cancellation/physical-evidence.json"
        )
        required["watchdog_physical"] = (
            "coordination/flow-9-rf-watchdog/physical-evidence.json"
        )
    if scenario == "full-suite":
        required.update(
            physical_recovery_ten="coordination/recovery/recovery-ten-physical.json",
            full_suite_handoff="coordination/full-suite-handoff.json",
            physical_full_suite="coordination/full-suite-physical.json",
            ledger_full_suite="full-suite-ledger.json",
            ledger_continuous10="continuous10-ledger.json",
            ledger_recovery_ten="recovery-ten-ledger.json",
            recovery_runtime="recovery-ten-runtime.log",
            raw_runtime=str(log.with_suffix(".raw.log")),
        )
        if platform == "ios":
            required["device_install"] = "device-install.json"
    if ios_pending:
        required.update(
            device_install="device-install.json",
            ledger_pending_reload="pending-reload-ledger.json",
            raw_runtime=str(log.with_suffix(".raw.log")),
            native_identity_log=str(log.with_suffix(".native-identity.log")),
            pending_projection="coordination/runtime.log",
        )
    if (
        platform == "android"
        or scenario in ("recovery-ten", "full-suite")
        or ios_pending
    ):
        required["runtime_capture"] = str(log.with_suffix(".capture.json"))
    if platform == "android":
        required["artifact_install_identity"] = "artifact-install-identity.json"
    return {role: by_name.get(name) for role, name in required.items()}


def _passed_structure(document: dict[str, Any]) -> None:
    _require(
        document.get("completed") is True
        and document.get("primary_error") is None
        and document.get("cleanup_errors") == []
        and document.get("evidence_errors") == [],
        "passed requires completion without primary/cleanup/evidence errors",
    )
    _require(isinstance(document.get("artifact"), dict), "missing frozen artifact")
    _require(
        document["flow_index"]["invalid_markers"] == [],
        "malformed/truncated suite structural marker",
    )
    _validate_scenario(document["platform"], document.get("scenario"))
    if document["scenario"] in (
        "write-reject",
        "ccc-enable-reject",
        "ccc-disable-reject",
    ):
        scenario = document["scenario"]
        index = document["flow_index"]
        _require(
            index["scenario"] == scenario
            and not index["error_markers"]
            and index["facts"] is not None
            and index["facts"]["passed"]
            and index["facts"]["closed"],
            f"incomplete/invalid {scenario} runtime",
        )
        _require(
            all(document["references"].values()),
            f"missing {scenario} evidence references",
        )
        return
    if document["scenario"] == "ios-adapter-startup":
        from tools.react_native.ios_adapter_startup_evidence import verify_structure

        verify_structure(document)
        return
    if document["scenario"] == "permission-transition":
        _require(
            all(document["references"].values()),
            "missing permission-transition references",
        )
        index = document["flow_index"]
        _require(
            not index["error_markers"] and not index.get("capture_error"),
            "permission-transition failed/invalid capture",
        )
        _require(
            [row["phase"] for row in index["flows"]]
            == list(SCENARIO_CATALOG["permission-transition"])
            and all(row["start_line"] is not None for row in index["flows"]),
            "permission-transition missing raw phases",
        )
        return
    if document["scenario"] == "adapter-transition":
        _require(
            all(document["references"].values()),
            "missing adapter-transition references",
        )
        _require(
            document["flow_index"]["error_markers"] == [],
            "adapter-transition failure markers",
        )
        return
    if document["scenario"] == "full-suite":
        _passed_full_structure(document)
        return
    if document["scenario"] == "pending-read-reload":
        _passed_pending_structure(document)
        return
    if document["scenario"] == "recovery-ten":
        _passed_recovery_structure(document)
        return
    expected = [
        [kind, index, mode]
        for index, mode in enumerate(MODES)
        for kind in ("started", "passed")
    ]
    _require(
        document["flow_index"]["events"] == expected,
        "ten flows must pass ordered exactly once",
    )
    flows = document["flow_index"]["flows"]
    _require(
        len(flows) == 10 and all(row["status"] == "passed" for row in flows),
        "missing/partial/failed flow",
    )
    runtime = document["flow_index"]["runtime_markers"]
    _require(
        len(runtime) == 2 and [row["phase"] for row in runtime] == ["start", "close"],
        "missing/duplicate runtime start/close",
    )
    _require(
        runtime[0]["session"] == runtime[1]["session"]
        and runtime[0]["nonce"] == runtime[1]["nonce"]
        and str(uuid.UUID(runtime[0]["session"])) == runtime[0]["session"],
        "runtime session/nonce drift",
    )
    summary = document["flow_index"]["summaries"]
    _require(
        len(summary) == 1
        and summary[0]["status"] == "passed"
        and summary[0]["passed_count"] == 10
        and summary[0]["failure_count"] == 0,
        "missing/duplicate final 10/10 summary",
    )
    _require(
        runtime[0]["line"] < flows[0]["start_line"]
        and flows[-1]["terminal_line"] < runtime[1]["line"] < summary[0]["line"],
        "runtime/flow/summary line order invalid",
    )
    _require(
        all(document["references"].values()), "missing required evidence references"
    )


def _passed_recovery_structure(document: dict[str, Any]) -> None:
    index = document["flow_index"]
    _require(
        index["scenario"] == "recovery-ten"
        and index["mode"] == "cycle"
        and index["prefix"] == RECOVERY_PREFIX
        and index["error_markers"] == [],
        "recovery-ten error markers or scenario drift",
    )
    events = index["events"]
    _require(
        [(row["event"], row["cycle"]) for row in events]
        == [(kind, cycle) for cycle in range(10) for kind in ("start", "passed")],
        "recovery-ten requires ten ordered cycles exactly once",
    )
    flows = index["flows"]
    _require(
        len(flows) == 10
        and [row["cycle"] for row in flows] == list(range(10))
        and all(row["status"] == "passed" for row in flows),
        "recovery-ten missing/partial/failed cycle",
    )
    runtime = index["runtime_markers"]
    _require(
        len(runtime) == 2 and [row["phase"] for row in runtime] == ["start", "close"],
        "recovery-ten requires one runtime start/close",
    )
    run_id = runtime[0]["run_id"]
    _require(
        str(uuid.UUID(run_id)) == run_id
        and runtime[1]["run_id"] == run_id
        and runtime[0]["nonce"] == runtime[1]["nonce"],
        "recovery-ten runtime runId/nonce drift",
    )
    summary = index["summaries"]
    _require(
        len(summary) == 1
        and summary[0]["status"] == "passed"
        and summary[0]["cycles"] == 10
        and summary[0]["run_id"] == run_id,
        "recovery-ten requires one same-run final ten-cycle passed summary",
    )
    resources = index["resources"]
    _require(
        [(row["cycle"], row["phase"]) for row in resources]
        == [(cycle, phase) for cycle in range(10) for phase in RECOVERY_PHASES],
        "recovery-ten requires fifty unique cycle/phase snapshots in order",
    )
    for row in resources:
        snapshot = row["snapshot"]
        _require(
            set(snapshot) == set(RECOVERY_RESOURCE_FIELDS)
            and all(
                type(value) is int and 0 <= value <= 9_007_199_254_740_991
                for value in snapshot.values()
            ),
            "recovery-ten invalid seventeen-field integer snapshot",
        )
        if row["phase"] == "closed":
            _require(
                all(value == 0 for value in snapshot.values()),
                "recovery-ten closed snapshot must be exactly zero",
            )
    for cycle, flow in enumerate(flows):
        start, passed = events[cycle * 2 : cycle * 2 + 2]
        samples = resources[cycle * 5 : cycle * 5 + 5]
        lines = [start["line"], *(row["line"] for row in samples), passed["line"]]
        _require(
            all(before < after for before, after in pairwise(lines))
            and flow["start_line"] == start["line"]
            and flow["terminal_line"] == passed["line"],
            "recovery-ten snapshot/start/pass line order invalid",
        )
        boundary = flows[cycle + 1]["start_line"] if cycle < 9 else runtime[1]["line"]
        _require(
            passed["line"] < boundary, "recovery-ten cycle crossed next start/close"
        )
        if cycle < 9:
            _require(
                flow["end_line"] == boundary - 1, "recovery-ten segment range mismatch"
            )
    _require(
        runtime[0]["line"] < flows[0]["start_line"]
        and flows[-1]["terminal_line"]
        < runtime[1]["line"]
        < summary[0]["line"]
        <= flows[-1]["end_line"],
        "recovery-ten runtime/cycle/summary line order invalid",
    )
    _require(
        all(document["references"].values()), "missing required evidence references"
    )


def _passed_pending_structure(document: dict[str, Any]) -> None:
    """Offline structure only; parent ledger owns native route/RF semantics."""
    index = document["flow_index"]
    _require(
        index["scenario"] == "pending-read-reload" and index["error_markers"] == [],
        "pending-reload error markers or scenario drift",
    )
    events = index["events"]

    def milestones(kind: str, generations: list[int]) -> list[dict[str, Any]]:
        rows = [event for event in events if event["event"] == kind]
        _require(
            [row.get("generation") for row in rows] == generations,
            f"pending-reload missing/duplicate/foreign {kind}",
        )
        return rows

    runtime = index["runtime_markers"]
    _require(
        len(runtime) == 2 and [row["generation"] for row in runtime] == [1, 2],
        "pending-reload requires two ordered runtime starts exactly once",
    )
    run_id = runtime[0]["run_id"]
    _require(
        str(uuid.UUID(run_id)) == run_id and runtime[1]["run_id"] == run_id,
        "pending-reload runtime runId drift/noncanonical UUID",
    )
    _require(
        runtime[0]["nonce"] != runtime[1]["nonce"],
        "pending-reload requires different JS boot nonces",
    )
    ready = milestones("native-ready", [1, 2])
    old_read = milestones("read-dispatched", [1])[0]
    native_close = milestones("native:engine-close-complete", [1])[0]
    new_read = milestones("read-passed", [2])[0]
    close = milestones("close-passed", [2])[0]
    summaries = index["summaries"]
    _require(
        len(summaries) == 1
        and summaries[0]["status"] == "passed"
        and summaries[0]["run_id"] == run_id,
        "pending-reload requires one same-run final passed summary",
    )
    for event in events:
        if event["event"].startswith("native:"):
            _require(
                event["run_id"] == run_id
                and str(uuid.UUID(event["probe_id"])) == event["probe_id"],
                "pending-reload foreign/noncanonical native identity",
            )
    flows = index["flows"]
    _require(
        len(flows) == 2
        and [row["generation"] for row in flows] == [1, 2]
        and all(row["status"] == "passed" for row in flows),
        "pending-reload requires exactly two passed generation segments",
    )
    _require(
        runtime[0]["line"] < ready[0]["line"] < old_read["line"] < native_close["line"]
        and old_read["line"] < runtime[1]["line"] < ready[1]["line"]
        and native_close["line"]
        < ready[1]["line"]
        < new_read["line"]
        < close["line"]
        < summaries[0]["line"],
        "pending-reload readiness/cleanup/read/close/summary order invalid",
    )
    _require(
        [row["start_line"] for row in flows] == [row["line"] for row in runtime]
        and [row["end_line"] for row in flows]
        == [native_close["line"], summaries[0]["line"]],
        "pending-reload segment terminal/range mismatch",
    )
    _require(
        all(document["references"].values()), "missing required evidence references"
    )


def _git_context(project_root: Path) -> dict[str, Any] | None:
    if not (project_root / ".git").exists():
        return None
    # No diff, paths or contents are published. An unavailable git is not proof.
    try:
        head = subprocess.run(
            ["git", "--no-pager", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout.strip()
        _require(
            bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)), "invalid git HEAD"
        )
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                ],
                cwd=project_root,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=3,
            )
            output.seek(0)
            dirty = bool(output.read(1))
        return {"head": head, "dirty": dirty, "source_to_build_attestation": False}
    except (OSError, EvidenceError, subprocess.SubprocessError):
        return {"available": False, "source_to_build_attestation": False}


class EvidenceBundle:
    """One private attempt's immutable installation candidate and raw evidence.

    platform is 'android' or 'ios'; device is the runner-selected serial/UDID.
    scenario defaults to 'continuous10'; 'pending-read-reload' supports both platforms.
    'recovery-ten' supports both platforms, one runtime and ten fresh-probe cycles.
    'full-suite' composes continuous10 and recovery-ten in the same raw stream.
    'write-reject' independently replays response-write REJECT; never continuous10.
    'ccc-enable-reject' independently replays CCC enable REJECT and notification recovery.
    'ccc-disable-reject' proves the original subscription survives rejected disable.
    'adapter-transition' is Android-only: off rejection then a fresh rf-io Probe.
    'permission-transition' binds three fresh Android processes and OS reason8 evidence.
    New schema v1 manifests always write an explicit scenario. Offline verification
    reads legacy v1 absence as continuous10 only, without backfilling history.
    Construction performs no build, git command or artifact inspection. It may
    create the attempt and atomically publish a running manifest; handle errors.
    """

    def __init__(
        self,
        attempt: Path,
        *,
        platform: str,
        device: str,
        project_root: Path,
        scenario: str = "continuous10",
        adapter_mode: str | None = None,
    ) -> None:
        _require(platform in ("android", "ios"), "unsupported platform")
        _validate_scenario(platform, scenario)
        _require(
            adapter_mode is None
            or scenario == "adapter-transition"
            and adapter_mode in ("startup-off", "active-loss"),
            "invalid explicit adapter mode",
        )
        _require(
            isinstance(device, str) and bool(device.strip()), "missing device identity"
        )
        _no_links(attempt.absolute())
        attempt.mkdir(parents=True, exist_ok=True)
        self.attempt = attempt.absolute()
        self.project_root = project_root.absolute()
        self.document: dict[str, Any] = {
            "schema_version": 1,
            "scenario": scenario,
            **({"adapter_mode": adapter_mode} if adapter_mode is not None else {}),
            "status": "running",
            "platform": platform,
            "device": device,
            "run_id": str(uuid.uuid4()),
            "attempt_name": attempt.name,
            "host": {
                "hostname": host_platform.node(),
                "system": host_platform.system(),
                "release": host_platform.release(),
                "machine": host_platform.machine(),
            },
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "artifact": None,
            "files": [],
            "references": {},
            "flow_index": None,
            "limitations": LIMITATIONS,
        }
        manifest = self.attempt / MANIFEST
        _require(
            not manifest.exists() and not manifest.is_symlink(),
            "attempt already has an evidence bundle",
        )
        _publish(manifest, self.document)

    def prepare_artifact(self, source: Path) -> Path:
        """Freeze the exact installer input; reject reuse, unsafe or drifting source.

        The source is hashed before/after copying, with cooperative stat signatures;
        the snapshot's complete fingerprint must equal both. Modes are retained.
        A failed preparation leaves no usable artifact binding (finish stays failed).
        """
        _require(self.document["artifact"] is None, "artifact already prepared")
        source = source.absolute()
        _safe_name(source.name)
        signature = _source_signature(source)
        before = _artifact(source, self.document["platform"])
        candidate = self.attempt / "candidate"
        candidate.mkdir()
        snapshot = candidate / source.name
        if self.document["platform"] == "android":
            _copy_file(source, snapshot)
        else:
            snapshot.mkdir()
            rows = _walk(source)
            for name, path, info in rows:
                target = snapshot / name
                if stat.S_ISDIR(info.st_mode):
                    target.mkdir()
                else:
                    _copy_file(path, target)
            for name, _, info in reversed(rows):
                if stat.S_ISDIR(info.st_mode):
                    (snapshot / name).chmod(stat.S_IMODE(info.st_mode))
            snapshot.chmod(stat.S_IMODE(source.lstat().st_mode))
        after = _artifact(source, self.document["platform"])
        frozen = _artifact(snapshot, self.document["platform"])
        _require(
            signature == _source_signature(source) and before == after == frozen,
            "source/snapshot changed during freeze",
        )
        _walk(self.attempt)  # One budget across raw evidence AND frozen candidate.
        self.document["artifact"] = {
            "path": snapshot.relative_to(self.attempt).as_posix(),
            "source_path": str(source),
            "source_signature": signature,
            "source_signature_sha256": _digest(signature),
            "source_fingerprint": before,
            "fingerprint": frozen,
        }
        _publish(self.attempt / MANIFEST, self.document)
        return snapshot

    def verify_artifact(self) -> None:
        """Rehash the entire frozen snapshot (not the mutable build source)."""
        _verify_candidate(
            self.attempt, self.document["artifact"], self.document["platform"]
        )

    def finish(
        self,
        *,
        completed: bool,
        primary_error: BaseException | None,
        cleanup_errors: list[str],
        runtime_log: Path,
    ) -> None:
        """Publish final manifest after runner RF checks and all cleanup.

        completed=True means the scenario's independent online checker passed;
        bundle structure/hash validation never substitutes for that checker.

        A would-be pass with missing/invalid evidence publishes failed then raises.
        For an already failed run, normal missing/partial evidence is recorded and
        does not mask its error. Unexpected read/publication exceptions always
        publish a best-effort failed manifest and re-raise for runner add_note().
        """
        document = self.document
        document.update(
            status="failed",
            completed=completed,
            primary_error=None
            if primary_error is None
            else {"type": type(primary_error).__name__, "message": str(primary_error)},
            cleanup_errors=list(cleanup_errors),
            evidence_errors=[],
            finished_at=datetime.now(UTC).isoformat(),
        )
        errors = document["evidence_errors"]
        wants_pass = completed and primary_error is None
        try:
            document["git"] = _git_context(self.project_root)
            _walk(self.attempt)
            document["files"] = _inventory(self.attempt)
            relative = _safe_name(
                runtime_log.absolute().relative_to(self.attempt).as_posix()
            )
            document["runtime_log"] = relative
            document["references"] = _references(
                document["files"], document["platform"], relative, document["scenario"]
            )
            if (
                runtime_log.exists()
                or document["scenario"] == "permission-transition"
                and (self.attempt / _permission_names()["raw_runtime"]).exists()
            ):
                document["flow_index"] = _flow_index(
                    runtime_log, relative, document["scenario"]
                )
                if not runtime_log.exists():
                    errors.append(
                        "missing runtime log; permission raw capture retained"
                    )
            else:
                document["flow_index"] = _empty_index(relative, document["scenario"])
                errors.append("missing runtime log")
            try:
                self.verify_artifact()
            except (EvidenceError, FileNotFoundError, zipfile.BadZipFile) as error:
                errors.append(str(error))
            try:
                _notification_observation_proofs(
                    self.attempt, document, complete=wants_pass
                )
                _passed_structure(document)
                if document["scenario"] == "write-reject":
                    _write_reject_proofs(self.attempt, document)
                if document["scenario"] == "ccc-enable-reject":
                    _ccc_enable_reject_proofs(self.attempt, document)
                if document["scenario"] == "ccc-disable-reject":
                    _ccc_disable_reject_proofs(self.attempt, document)
                if document["scenario"] == "permission-transition":
                    _permission_proofs(self.attempt, document)
                if document["scenario"] == "adapter-transition":
                    _adapter_proofs(self.attempt, document)
                if document["scenario"] == "ios-adapter-startup":
                    from tools.react_native.ios_adapter_startup_evidence import (
                        verify_hooks,
                    )

                    verify_hooks(self.attempt, document)
                if document["scenario"] == "full-suite":
                    _full_proofs(self.attempt, document)
                if (
                    document["scenario"] == "pending-read-reload"
                    and document["platform"] == "ios"
                ):
                    _ios_pending_proofs(self.attempt, document)
            except (ValueError, KeyError, TypeError) as error:
                errors.append(str(error))
            if wants_pass and not errors:
                document["status"] = "passed"
            _publish(self.attempt / MANIFEST, document)
            if document["status"] == "passed":
                verify_bundle(self.attempt / MANIFEST)
            if wants_pass and errors:
                raise EvidenceError("evidence bundle cannot pass: " + "; ".join(errors))
        except Exception as error:
            document["status"] = "failed"
            errors.append(f"finish: {type(error).__name__}: {error}")
            try:
                _publish(self.attempt / MANIFEST, document)
            except (OSError, ValueError, TypeError, RuntimeError) as publication_error:
                error.add_note(
                    f"failed manifest publication also failed: {publication_error}"
                )
            raise


def _verify_candidate(attempt: Path, artifact: Any, platform: str) -> None:
    _require(isinstance(artifact, dict), "missing frozen artifact")
    _require(
        _digest(artifact["source_signature"]) == artifact["source_signature_sha256"],
        "source signature hash drift",
    )
    relative = _safe_name(artifact["path"])
    _require(
        relative.startswith("candidate/") and len(relative.split("/")) == 2,
        "candidate path must be a direct snapshot child",
    )
    candidate = attempt / "candidate"
    _no_links(candidate)
    _require(
        sorted(item.name for item in candidate.iterdir()) == [relative.split("/")[1]],
        "candidate directory inventory changed",
    )
    actual = _artifact(attempt / relative, platform)
    _require(
        actual == artifact["fingerprint"] == artifact["source_fingerprint"],
        "frozen artifact fingerprint drift",
    )


def _metadata(document: dict[str, Any]) -> None:
    _require(
        isinstance(document.get("device"), str) and bool(document["device"].strip()),
        "missing device identity",
    )
    name = _safe_name(document["attempt_name"])
    _require("/" not in name, "invalid attempt identity")
    host = document.get("host")
    _require(
        isinstance(host, dict)
        and set(host) == {"hostname", "system", "release", "machine"}
        and all(isinstance(value, str) for value in host.values()),
        "missing host identity",
    )
    started = datetime.fromisoformat(document["started_at"])
    finished = datetime.fromisoformat(document["finished_at"])
    _require(
        started.tzinfo is not None
        and finished.tzinfo is not None
        and finished >= started,
        "missing/invalid run time range",
    )
    _require(type(document.get("completed")) is bool, "missing completion state")
    for key in ("cleanup_errors", "evidence_errors"):
        _require(
            isinstance(document.get(key), list)
            and all(isinstance(value, str) for value in document[key]),
            f"invalid {key}",
        )
    error = document.get("primary_error")
    _require(
        error is None
        or (
            isinstance(error, dict)
            and set(error) == {"type", "message"}
            and all(isinstance(value, str) for value in error.values())
        ),
        "invalid primary error",
    )


def _legacy_write_reject_index_failure(
    document: dict[str, Any], index: dict[str, Any]
) -> bool:
    # Before FixtureSessionError was caught by the indexer, finish inventoried
    # bytes but published its initial null index. Accept only that exact failed
    # shape and a replayed malformed-JSON diagnostic; never repair the manifest.
    errors = index.get("error_markers", [])
    return (
        document["scenario"] == "write-reject"
        and document["status"] == "failed"
        and document["completed"] is False
        and document["flow_index"] is None
        and index.get("facts") is None
        and len(errors) == 1
        and errors[0].startswith("malformed session JSON:")
        and document["evidence_errors"] == [f"finish: FixtureSessionError: {errors[0]}"]
    )


def verify_bundle(path: Path) -> None:
    """Offline hash/flow/structure validation, including partial failed bundles.

    No device access, signature trust, source rebuild or live physical validation.
    Permission-transition replays recorded runtime/OS/physical facts through the
    runner's shared ledger; that audit cannot prove current device/fixture state.
    Legacy schema v1 without scenario uses continuous10 in a read-only local copy;
    all artifact/reference/ten-flow checks remain strict. Nothing is written back.
    Manifest itself is intentionally not hashed; this is not adversarial attestation.
    A failed manifest with absent candidate/log is valid if its recorded references
    are absent too; any present fingerprint must still verify, even on failure.
    """
    document = _json(path)
    _require(
        document.get("schema_version") == 1
        and document.get("status") in ("passed", "failed"),
        "unsupported schema or unfinished bundle",
    )
    # Schema v1 predates scenario dispatch. Only absence gets the legacy default;
    # explicit unknown/null values remain invalid, and pending logs cannot guess it.
    document = {**document, "scenario": document.get("scenario", "continuous10")}
    attempt = path.absolute().parent
    _require(path.name == MANIFEST, f"expected {MANIFEST}")
    _require(document.get("platform") in ("android", "ios"), "invalid platform")
    _validate_scenario(document["platform"], document.get("scenario"))
    _require(
        str(uuid.UUID(document["run_id"])) == document["run_id"], "invalid run identity"
    )
    _require(document.get("limitations") == LIMITATIONS, "missing evidence limitations")
    _metadata(document)
    _walk(attempt)
    actual_files = _inventory(attempt)
    _require(actual_files == document["files"], "raw evidence inventory/hash drift")
    if document.get("artifact") is not None:
        _verify_candidate(attempt, document["artifact"], document["platform"])
    elif (attempt / "candidate").exists():
        # A failed freeze can leave an unbound partial copy. Never claim its hash.
        _require(document["status"] == "failed", "unbound candidate")
        _walk(attempt / "candidate")
    relative = _safe_name(document["runtime_log"])
    _require(
        relative != MANIFEST and relative.split("/")[0] != "candidate",
        "invalid runtime log path",
    )
    _require(
        _references(actual_files, document["platform"], relative, document["scenario"])
        == document["references"],
        "evidence hash references drift",
    )
    if (
        (attempt / relative).exists()
        or document["scenario"] == "permission-transition"
        and (attempt / _permission_names()["raw_runtime"]).exists()
    ):
        _require(
            (attempt / relative).exists() or document["status"] == "failed",
            "missing permission runtime projection",
        )
        index = _flow_index(attempt / relative, relative, document["scenario"])
        _require(
            index == document["flow_index"]
            or _legacy_write_reject_index_failure(document, index),
            "flow ranges/segment hashes/runtime identity drift",
        )
    else:
        _require(document["status"] == "failed", "missing runtime log")
        _require(
            document["flow_index"] == _empty_index(relative, document["scenario"]),
            "invalid missing flow index",
        )
    _notification_observation_proofs(
        attempt, document, complete=document["status"] == "passed"
    )
    if document["status"] == "passed":
        _passed_structure(document)
        if document["scenario"] == "write-reject":
            _write_reject_proofs(attempt, document)
        if document["scenario"] == "ccc-enable-reject":
            _ccc_enable_reject_proofs(attempt, document)
        if document["scenario"] == "ccc-disable-reject":
            _ccc_disable_reject_proofs(attempt, document)
        if document["scenario"] == "permission-transition":
            _permission_proofs(attempt, document)
        if document["scenario"] == "adapter-transition":
            _adapter_proofs(attempt, document)
        if document["scenario"] == "ios-adapter-startup":
            from tools.react_native.ios_adapter_startup_evidence import verify_hooks

            verify_hooks(attempt, document)
        if document["scenario"] == "full-suite":
            _full_proofs(attempt, document)
        if (
            document["scenario"] == "pending-read-reload"
            and document["platform"] == "ios"
        ):
            _ios_pending_proofs(attempt, document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        verify_bundle(args.manifest)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"evidence-bundle: invalid: {error}", file=sys.stderr)
        return 1
    print("evidence-bundle: integrity verified (not RF reauthorization)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
