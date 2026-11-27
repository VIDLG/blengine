"""The two primitives every iOS native-instrumentation lane needs, and nothing else.

The Android lanes get these from `android_sdk` + logcat; an iOS lane hosted by a signed XCTest
bundle needs the same two things:

- **stage markers**: the analogue of `adb logcat -s TAG:I` is `idevicesyslog -u <udid>`, a
  read-only relay of the device's own syslog. Markers are diagnostics, never a result;
- **a staged `.xctestrun`**: the only place `xcodebuild test-without-building` accepts
  per-run test-process environment, and therefore the only place a peer declaration can go
  without rebuilding the signed bundle.

Both are bounded and refuse anything they cannot verify. Neither touches BLE.
"""

from __future__ import annotations

import functools
import math
import plistlib
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import host_command
from tools.apple.app_artifact import fingerprint as artifact_fingerprint

MARKER_TAG_VARIABLE = "BLEHUB_MULTI_LINK"
DECLARATION_VARIABLE = "BLEHUB_MULTI_PEERS"
# The scene-level fault declaration, in the same `<kind>:<peer>` spelling the host CLI takes.
# Absent means no fault, which is a declaration too: the test treats an unknown spelling as a
# failed scene, never as "none".
FAULT_VARIABLE = "BLEHUB_MULTI_FAULT"
# Owned by `AppleNotificationDiagnostics` in the Apple backend: it writes one JSON line per
# notification event (`value.entry`, `value.disposition`, ...) to the test process's stderr, which
# lands in the lane's xcodebuild log. Off unless a run asks for it.
APPLE_NOTIFICATION_DIAGNOSTICS_VARIABLE = "BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS"
# What `xcodebuild test-without-building` prints when the selected tests all pass.
RESULT_SUCCEEDED = "** TEST SUCCEEDED **"
# What `xcodebuild test-without-building` prints when the selected tests all pass. Under `-quiet`
# it prints nothing, so a run's verdict is its exit code; this only exists for non-quiet logs.
RESULT_SUCCEEDED = "** TEST SUCCEEDED **"
MAX_LOG_BYTES = 4 * 1024 * 1024
# How long the relay is charged once, before a test starts, after its first device line.
# `idevicesyslog` attaches asynchronously and the lines around attach are not yet the steady
# state: on device a scene lost exactly its opening markers when launched as soon as the
# first line arrived, and kept all of them when run a moment later. The scene also opens with a
# probe burst (`PROBE_TAG`) so the window is absorbed by lines that are not stages.
WARMUP_SECONDS = 1.5


@functools.lru_cache(maxsize=8)
def _stage_pattern(tag: str) -> re.Pattern[str]:
    """Match this exact tag, a delimiter, then the stage.

    The same wire shape as the Android lane's `logcat` markers (`TAG: stage`). The delimiter is
    what keeps a line that merely mentions the tag from becoming a stage, and it is why this is
    not a generic token scan.
    """
    return re.compile(rf"\b{re.escape(tag)}\s*:\s*(?P<stage>[A-Za-z][^\s].*?)\s*$")


def require_executable(name: str) -> str:
    """Resolve one host tool a lane needs, before it leases, installs or starts anything.

    A missing relay or launcher fails today only when the process is spawned — after a phone
    lease and an App install have already been spent. Naming the tool up front makes the
    absence a precondition instead of a mid-run accident.
    """
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required host executable: {name}")
    return executable


class SyslogMarkerCapture:
    """Live stage markers from the device syslog, bounded and never a result source.

    The process is read-only and scoped to one UDID. A marker is only accepted when an exact
    tag token is followed by the stage, so an unrelated line that merely contains the tag
    cannot become a stage.
    """

    def __init__(self, device: str, deadline: float, *, tag: str) -> None:
        if not tag or not tag.replace("_", "").isalnum():
            raise ValueError("marker tag must be one alphanumeric token")
        self._tag = tag
        self._deadline = deadline
        self._lines: list[str] = []
        self._received = 0
        self._opened_at = time.monotonic()
        self._first_line_at: float | None = None
        self._warmup_seconds = 0.0
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [require_executable("idevicesyslog"), "-u", device],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            return
        with stdout:
            for line in stdout:
                with self._lock:
                    self._received += 1
                    if self._first_line_at is None:
                        self._first_line_at = time.monotonic()
                if self._tag not in line:
                    continue
                with self._lock:
                    if sum(len(item) + 1 for item in self._lines) < MAX_LOG_BYTES:
                        self._lines.append(line.rstrip("\r\n"))

    def wait_live(self, deadline: float, *, warmup: float = WARMUP_SECONDS) -> None:
        """Block until the relay is attached and past its startup window.

        The relay attaches asynchronously, and arrival of its first line is not yet evidence of a
        steady channel: a scene launched at that instant lost its opening markers, while the same
        scene run a moment later kept them all. One declared warm-up, charged once, is cheaper
        than making every stage idempotent — and a run that cannot afford it is refused here
        rather than started blind.
        """
        if not math.isfinite(warmup) or warmup < 0:
            raise ValueError("relay warm-up must be finite and non-negative")
        while True:
            with self._lock:
                if self._received:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the device syslog relay never became live")
            time.sleep(min(0.05, remaining))
        settle = time.monotonic() + warmup
        if settle >= deadline:
            raise TimeoutError(
                "the device syslog relay cannot complete its warm-up inside the run budget"
            )
        self._warmup_seconds = warmup
        while True:
            remaining = settle - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(remaining)

    def attach_seconds(self) -> float | None:
        """How long the relay took to deliver a first device line, or None if it never did."""
        with self._lock:
            first = self._first_line_at
        return None if first is None else first - self._opened_at

    @property
    def warmup_seconds(self) -> float:
        """The warm-up this relay was actually charged, for the run's evidence."""
        return self._warmup_seconds

    def stages(self) -> list[str]:
        """Ordered stage markers, parsed from the captured lines only."""
        with self._lock:
            lines = list(self._lines)
        found = []
        pattern = _stage_pattern(self._tag)
        for line in lines:
            match = pattern.search(line)
            if match is None:
                continue
            found.append(match.group("stage").strip())
        return found

    def log(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def lines_containing(self, text: str) -> int:
        """How many captured lines carry `text`, for channel diagnostics such as the probe burst."""
        with self._lock:
            return sum(1 for line in self._lines if text in line)

    def close(self) -> None:
        host_command.stop(self._process)
        self._reader.join(timeout=5)


def _target_key(document: dict[str, Any], test_bundle: str) -> str:
    for key, value in document.items():
        if not isinstance(value, dict):
            continue
        bundle = value.get("TestBundlePath")
        if isinstance(bundle, str) and bundle.rstrip("/").endswith(test_bundle):
            return key
    raise ValueError(f"xctestrun carries no target for {test_bundle}")


@dataclass(frozen=True)
class XctestrunTarget:
    """One resolved test target: the manifest's own names, expanded to absolute paths.

    `name` is the xctestrun key, which is also the target name `-only-testing` expects. Reading
    it back from the manifest instead of repeating it is what keeps the bundle spelling
    (`Something.xctest`) and the target spelling (`Something`) from drifting apart.
    """

    name: str
    host_app: Path
    bundle: Path
    document: dict[str, Any]


def resolve_xctestrun(
    source: Path,
    *,
    test_bundle: str,
    expected_app: Path | None = None,
    expected_host_bundle_id: str | None = None,
) -> XctestrunTarget:
    """Read one manifest into the absolute facts a run needs, refusing what it cannot verify.

    A `build-for-testing` manifest writes `__TESTROOT__` / `__TESTHOST__` relative to where it was
    produced. The staged copy lives elsewhere, so those are expanded against the source's own
    products directory first; leaving them unexpanded is how a device run resolves a path that
    does not exist.

    `test-without-building` installs the product the manifest names, not the one the operator
    passed. So a manifest naming another App, or another test-host bundle identifier, is refused
    here rather than discovered after an install.
    """
    document = plistlib.loads(source.read_bytes())
    if not isinstance(document, dict):
        raise TypeError("xctestrun is not a plist dictionary")
    document = dict(document)
    key = _target_key(document, test_bundle)
    target = dict(document[key])
    products = source.parent.resolve()

    # `TestHostPath` must be absolute before `__TESTHOST__` can be expanded against it.
    host_path = target.get("TestHostPath")
    if isinstance(host_path, str) and host_path:
        target["TestHostPath"] = host_path.replace("__TESTROOT__", str(products))

    def expanded(value: object) -> object:
        """Expand every product path in the target, not only its host and bundle.

        A UI test entry also names `UITargetAppPath` (and may name other product paths);
        leaving `__TESTROOT__` there makes a *staged* manifest resolve the App under the
        staging directory, which xcodebuild reports as a missing App.
        """
        if isinstance(value, str):
            return _expand_testhost(
                value.replace("__TESTROOT__", str(products)), target
            )
        if isinstance(value, list):
            return [expanded(item) for item in value]
        if isinstance(value, dict):
            return {name: expanded(item) for name, item in value.items()}
        return value

    for name, value in list(target.items()):
        if name == "TestHostPath":
            continue
        target[name] = expanded(value)
    host = target.get("TestHostPath")
    if not isinstance(host, str) or not host:
        raise ValueError(f"xctestrun target {key!r} declares no test host")
    bundle = target.get("TestBundlePath")
    if not isinstance(bundle, str) or not bundle:
        raise ValueError(f"xctestrun target {key!r} declares no test bundle")
    host_app = Path(host).resolve()
    if expected_app is not None and host_app != expected_app.resolve():
        raise ValueError(
            f"xctestrun test host is not the signed App this run authorized: {host!r}"
        )
    if expected_host_bundle_id is not None:
        declared = target.get("TestHostBundleIdentifier")
        if declared != expected_host_bundle_id:
            raise ValueError(
                "xctestrun test host bundle identifier is not the App under test: "
                f"{declared!r} != {expected_host_bundle_id!r}"
            )
    document[key] = target
    return XctestrunTarget(
        name=key,
        host_app=host_app,
        bundle=Path(bundle).resolve(),
        document=document,
    )


def staged_xctestrun(
    source: Path,
    destination: Path,
    *,
    test_bundle: str,
    environment: dict[str, str],
    expected_app: Path | None = None,
    expected_host_bundle_id: str | None = None,
) -> Path:
    """Copy `source` with the test-process environment replaced by `environment`.

    Refuses a stale value for a key this host owns rather than silently repairing it. Which
    target is ours, and which App it hosts, are `resolve_xctestrun`'s answers — this function
    only adds the environment the manifest cannot carry.
    """
    resolved = resolve_xctestrun(
        source,
        test_bundle=test_bundle,
        expected_app=expected_app,
        expected_host_bundle_id=expected_host_bundle_id,
    )
    document = dict(resolved.document)
    target = dict(document[resolved.name])
    current = target.get("EnvironmentVariables", {})
    if not isinstance(current, dict):
        raise TypeError("xctestrun test environment is not a dictionary")
    merged = dict(current)
    for name, value in environment.items():
        if name in merged and merged[name] != value:
            raise ValueError(f"stale xctestrun value for {name}")
        merged[name] = value
    target["EnvironmentVariables"] = merged
    document[resolved.name] = target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plistlib.dumps(document, sort_keys=False))
    return destination


def _expand_testhost(value: str, target: dict[str, Any]) -> str:
    """`__TESTHOST__` is the host product itself — the `.app`, not its directory.

    A hosted bundle lives at `<host>.app/PlugIns/<bundle>`, so expanding to the parent
    directory produces `<Products>/PlugIns/<bundle>`, a path that does not exist.
    """
    if "__TESTHOST__" not in value:
        return value
    host = target.get("TestHostPath")
    if not isinstance(host, str) or "__TESTHOST__" in host:
        raise ValueError("xctestrun uses __TESTHOST__ before its host path is resolved")
    return value.replace("__TESTHOST__", str(Path(host).resolve()))


def app_bundle_id(app: Path) -> str:
    """The identity an installed App answers to, read from the bundle it was signed as."""
    info = plistlib.loads((app / "Info.plist").read_bytes())
    bundle_id = info.get("CFBundleIdentifier")
    if type(bundle_id) is not str or not bundle_id:
        raise ValueError("App Info.plist does not declare a bundle identifier")
    return bundle_id


def tree_sha256(root: Path) -> str:
    """One stable digest for a signed `.app` tree, path and content included.

    This is a thin alias, not a second algorithm: `tools.apple.app_artifact.fingerprint`
    owns the App-artifact contract (link refusal, regular-file checks, identity rechecks
    around every read). A separate local implementation silently disagreed with the
    freeze check in `bleng_ios_scan`, so no reviewed App could ever pass it.
    """
    return artifact_fingerprint(root)


def stage_environment(*, declaration: str, fault: str | None = None) -> dict[str, str]:
    """The facts the hosted test may not infer: opt-in, topology, and any declared fault."""
    environment = {
        MARKER_TAG_VARIABLE: "multi-link",
        DECLARATION_VARIABLE: declaration,
    }
    if fault is not None:
        environment[FAULT_VARIABLE] = fault
    return environment
