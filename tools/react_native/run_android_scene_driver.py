"""Drive one host-owned scene plan on a physical Android device, in-process.

This is the Android executor of the shared plan vocabulary in `scene_plans`: the same steps the
iOS XCTest target runs (`wait`, `waitAny`, `still`, `tap`, `tapWhen`, `tapUntil`, `read`,
`setText`, `activate`, `answerAlert`), executed by `BlengSceneDriverInstrumentedTest` with
`UiDevice` instead of by a WebDriver session. No Appium server, no UiAutomator2 server and no
HTTP hop are involved; the plan is a file the host pushes and the receipt is a file the host
reads back.

Two device facts shape this module:

* the instrumentation process runs under its own UID, so device files are written through
  `UiAutomation.executeShellCommand` (shell identity) and read back with `adb exec-out cat`;
* a failed step fails the JUnit test, so the receipt must be recovered *after* a failed
  instrumentation run — the receipt is what explains which step failed and what it observed.

Nothing here is a gate verdict: it reports the driver's own per-step observations and timings.
The host compares them with what the scene required.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tools import host_command
from tools.android import adb

from . import android_sdk
from .bleng_android_instrumentation import (
    HOST_MARKER_TAG,
    TEST_COMPONENT,
    MarkerCapture,
    instrument_command,
)
from .capture_io import publish_new_json
from .host_paths import PROJECT_ROOT
from .scene_plans import plans
from .scene_receipt import ANDROID_RECEIPT_SCOPE, decode_projection, require_receipt

SCOPE = "formal-android-scene-driver"
TEST_CLASS = "com.blehubblenghost.BlengSceneDriverInstrumentedTest"
METHOD = "scenePlan"
# The instrumentation process runs as the App under test, so the driver publishes into the
# App's external files directory and the host reads it back as the shell user. Nothing depends
# on the App being a debug build (`run-as`). All three files are removed before a run and
# published whole (written beside, then renamed), so a file that exists is this run's and is
# complete.
RECEIPT_FILE = "scene-receipt.json"
HIERARCHY_FILE = "scene-hierarchy.xml"
# The driver's last file, on a failed step too: the host stops the process once it exists.
DONE_FILE = "scene-done"
RUN_FILES = (RECEIPT_FILE, HIERARCHY_FILE, DONE_FILE)
HANDOFF_PREFIX = "handoff-"


def _files_path(app_id: str, name: str) -> str:
    return f"/sdcard/Android/data/{app_id}/files/{name}"


# The test class's own logcat tag; markers are diagnostics, never a result. The capture is
# scoped to one run by a sentinel the host writes under its own tag (`HOST_MARKER_TAG`): the
# stream replays the device's whole buffer first, the replay arrives in bursts, and clearing
# the buffer would destroy diagnostics this lane does not own.
LOG_TAG = "BLEHUB_RN_SCENE"
# Shell-owned scratch directory for the plan itself.
DEVICE_ROOT = "/data/local/tmp/blehub-scene"
TIMEOUT_MAX = 1800
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_HIERARCHY_BYTES = 64 * 1024
POLL_SECONDS = 0.2
# After the driver publishes its done file, the runner gets this long to exit on its own
# before the resident instrumentation process is stopped.
_LINGER_SECONDS = 3.0
_LINGER_POLLS = 15
_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
_ACTIVITY = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_$]*)+")
_PLAN_DIR = re.compile(r"[A-Za-z0-9._-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _remaining(deadline: float, label: str) -> float:
    left = deadline - time.monotonic()
    if not math.isfinite(left) or left <= 0:
        raise RuntimeError(f"scene driver deadline expired before {label}")
    return left


def _plan_dir(run_id: str, plan_name: str) -> str:
    if _PLAN_DIR.fullmatch(plan_name) is None:
        raise ValueError(f"unsafe plan name for a device path: {plan_name!r}")
    return f"{DEVICE_ROOT}/{run_id}/{plan_name}"


def _test_file(
    device: str, app_id: str, name: str, *, timeout: float, cwd: Path
) -> bytes:
    """Read one file the driver wrote, through `exec-out`, bounded."""
    path = _files_path(app_id, name)
    result = adb.run_command(
        adb.adb_command(device, "exec-out", "cat", path),
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
    raw = result.stdout.encode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(
            f"could not read {path} from the device "
            f"(exit={result.returncode}): {raw[-600:]!r}"
        )
    if len(raw) > MAX_RECEIPT_BYTES:
        raise RuntimeError(f"device file exceeds the 1 MiB bound: {path}")
    return raw


def _files(device: str, app_id: str, *, cwd: Path) -> set[str]:
    """The names currently in the driver's files directory; empty while it does not exist."""
    result = adb.run_command(
        adb.adb_command(device, "shell", "ls", _files_path(app_id, "")),
        cwd=cwd,
        timeout=30,
        check=False,
    )
    if result.returncode:
        return set()
    return {name.strip() for name in result.stdout.splitlines() if name.strip()}


def _run_files(names: set[str]) -> list[str]:
    """What a previous or interrupted run left behind: ours to remove before driving."""
    prefixes = (*RUN_FILES, HANDOFF_PREFIX)
    return sorted(name for name in names if name.startswith(prefixes))


def _remove_run_files(device: str, app_id: str, *, cwd: Path) -> None:
    """Remove the driver's files and every handoff exchange from the device."""
    # One string argument: adb hands it to the device shell, which expands the globs. Passing
    # `sh -c <pattern>` as separate argv would make the device shell split the command.
    patterns = " ".join(
        _files_path(app_id, f"{name}*") for name in (*RUN_FILES, f"{HANDOFF_PREFIX}*")
    )
    adb.run_command(
        adb.adb_command(device, "shell", f"rm -f {patterns}"),
        cwd=cwd,
        timeout=30,
        check=True,
    )


def _clear_test_files(device: str, app_id: str, *, cwd: Path) -> None:
    """Leave the driver's directory without a previous run's files, and prove it.

    The done file is the host's completion signal and a handoff request is its call to act,
    so one stale file from an interrupted run would be obeyed as this run's. Removal is
    therefore read back: a file that survives is a failure before anything is driven.
    """
    _remove_run_files(device, app_id, cwd=cwd)
    stale = _run_files(_files(device, app_id, cwd=cwd))
    if stale:
        raise RuntimeError(
            f"the scene driver's directory still holds a previous run's files: {stale}"
        )


def _remove_device_dir(device: str, directory: str, *, cwd: Path) -> None:
    """Best effort: a leftover scratch directory is not a scene failure."""
    try:
        adb.run_command(
            adb.adb_command(device, "shell", "rm", "-rf", directory),
            cwd=cwd,
            timeout=30,
            check=False,
        )
    except (OSError, RuntimeError):
        pass


def _recover_receipt(
    device: str, app_id: str, *, cwd: Path, deadline: float
) -> dict[str, Any]:
    raw = _test_file(
        device,
        app_id,
        RECEIPT_FILE,
        timeout=_remaining(deadline, "receipt read"),
        cwd=cwd,
    )
    if not raw.lstrip().startswith(b"{"):
        # `exec-out` answers a missing file with exit 0 and the shell's complaint on stdout.
        raise RuntimeError(
            f"the scene driver published no readable {RECEIPT_FILE}: {raw[-300:]!r}"
        )
    payload = json.loads(raw)
    if type(payload) is not dict:
        raise RuntimeError("scene receipt is not a JSON object")
    return payload


def _requested_handoffs(names: set[str]) -> list[str]:
    """Which host actions the driver has announced and is still waiting for.

    The driver writes one `handoff-request-<action>` file per occurrence. This is a device
    read rather than a log line on purpose: a long scene can outlive the host's logcat
    capture, and a handoff that only exists in a dead stream is a deadlock.
    """
    prefix = f"{HANDOFF_PREFIX}request-"
    return sorted(
        name[len(prefix) :]
        for name in names
        if name.startswith(prefix) and _PLAN_DIR.fullmatch(name[len(prefix) :])
    )


def _ack_handoff(
    device: str, app_id: str, action: str, *, deadline: float, cwd: Path
) -> None:
    """Tell the waiting driver that this host action finished.

    The driver deletes the acknowledgement before it announces the next action, so a scene
    that repeats one action (a drop per cycle) never mistakes an old file for a new answer.
    """
    if _PLAN_DIR.fullmatch(action) is None:
        raise RuntimeError(f"unsafe handoff action: {action!r}")
    adb.run_command(
        adb.adb_command(
            device, "shell", "touch", _files_path(app_id, f"handoff-ack-{action}")
        ),
        cwd=cwd,
        timeout=_remaining(deadline, "handoff acknowledgement"),
        check=True,
    )


def _handoff_requested(names: set[str], action: str) -> bool:
    """Is this exact host action still announced and unanswered?

    The driver deletes the request when it consumes the acknowledgement, so a request that
    is gone has been answered. That is what lets one scene perform the same action once per
    cycle: the file, not a remembered action name, is the occurrence.
    """
    return f"{HANDOFF_PREFIX}request-{action}" in names


def _await_handoff_consumed(
    device: str, app_id: str, action: str, *, deadline: float, cwd: Path
) -> None:
    """Wait until the driver has taken this occurrence's acknowledgement."""
    while _handoff_requested(_files(device, app_id, cwd=cwd), action):
        if time.monotonic() + POLL_SECONDS >= deadline:
            raise RuntimeError(f"the driver never consumed the {action!r} handoff")
        time.sleep(POLL_SECONDS)


def _drive_scene(
    device: str,
    arguments: dict[str, str],
    *,
    capture: MarkerCapture,
    app_id: str,
    deadline: float,
    guard: Callable[[], None],
    on_handoff: Callable[[str], None],
    cwd: Path,
    log: Path,
) -> dict[str, Any]:
    """Run one `am instrument` and stop when its receipt is published.

    The receipt, not the runner's exit, is this lane's contract. A JUnit run can publish
    its receipt and then leave the instrumentation process resident (the process also hosts
    the App's Activity), so waiting only for process death turns a finished scene into a
    timeout. The done file is the driver's last act, after the receipt and the hierarchy
    dump, so it is the point at which nothing more can arrive. It is a file this run created
    — the directory was verified empty first — where a logcat line may be a replayed one
    from an earlier scene.

    A process that exits on its own must still be a clean, single passing test; one that had
    to be stopped after publishing is recorded as `lingered` instead of pretending it was.
    """
    command = instrument_command(
        device, arguments, test_class=TEST_CLASS, method=METHOD
    )
    started = time.monotonic()
    guard()
    capture.require_baseline(deadline)
    with log.open("x", encoding="utf-8") as stream:
        try:
            process = adb.start_command(command)
        except adb.AdbError:
            raise RuntimeError("could not start the instrumentation") from None
        lingered = False
        handoffs: list[str] = []
        pending: set[str] = set()
        try:
            while process.poll() is None:
                _remaining(deadline, "instrumentation")
                names = _files(device, app_id, cwd=cwd)
                for action in _requested_handoffs(names):
                    # A repeating scene performs the same action once per cycle, so the
                    # request's own file is the occurrence: it is answered once and then
                    # deleted by the driver before the next one can be announced.
                    if action in pending:
                        continue
                    pending.add(action)
                    # The driver is now waiting on the host; the envelope's own authority
                    # guard brackets the action, and the acknowledgement is what lets the
                    # App continue, so the ordering is exact in both directions.
                    try:
                        guard()
                        on_handoff(action)
                        guard()
                        _ack_handoff(device, app_id, action, deadline=deadline, cwd=cwd)
                        _await_handoff_consumed(
                            device, app_id, action, deadline=deadline, cwd=cwd
                        )
                    finally:
                        pending.discard(action)
                    handoffs.append(action)
                if DONE_FILE in names:
                    # Give the runner its bounded grace, then stop the resident process.
                    for _ in range(_LINGER_POLLS):
                        if process.poll() is not None:
                            break
                        time.sleep(_LINGER_SECONDS / _LINGER_POLLS)
                    if process.poll() is None:
                        lingered = True
                        host_command.stop(process)
                    break
                time.sleep(POLL_SECONDS)
        finally:
            host_command.stop(process)
            raw = process.stdout.read() if process.stdout else b""
            output = raw.decode("utf-8", errors="replace")
            stream.write(output)
            stream.flush()
    return {
        "returncode": process.returncode,
        "elapsed_seconds": time.monotonic() - started,
        "lingered": lingered,
        "handoffs": handoffs,
        # Bounded diagnostics: the markers this run logged, so a handoff that never reached
        # the host can be told apart from one the host answered too late.
        "markers": capture.stages()[-200:],
        "output": output,
    }


def _exit_verdict(summary: dict[str, Any]) -> RuntimeError | None:
    """What the runner's own exit says, once the caller has recorded everything.

    A process that was stopped after publishing has no exit to judge. One that left on its
    own must have been a clean, single passing test.
    """
    if summary["lingered"]:
        return None
    if "OK (1 test)" not in summary["output"]:
        return RuntimeError("instrumentation did not report exactly one passing test")
    if summary["returncode"]:
        return RuntimeError(f"instrumentation exited {summary['returncode']}")
    return None


def run_scene(
    *,
    device: str,
    plan_name: str,
    output_root: Path,
    timeout: float,
    launch: bool,
    app_id: str,
    activity: str,
    test_apk_sha256: str,
    fresh_install: bool = False,
    expected_local_name: str | None = None,
    service_filter: str | None = None,
    steps: Sequence[dict[str, Any]] | None = None,
    cwd: Path,
    run_id: str | None = None,
    require_owner: Callable[[], None] | None = None,
    on_handoff: Callable[[str], None] | None = None,
) -> Path:
    """Drive one plan; publish a result and return its path."""
    if type(device) is not str or _DEVICE.fullmatch(device) is None:
        raise ValueError("an explicit physical Android serial is required")
    if type(app_id) is not str or _PACKAGE.fullmatch(app_id) is None:
        raise ValueError("an explicit canonical Android App package is required")
    if type(activity) is not str or _ACTIVITY.fullmatch(activity) is None:
        raise ValueError("an explicit fully qualified Android activity is required")
    if type(test_apk_sha256) is not str or _SHA256.fullmatch(test_apk_sha256) is None:
        raise ValueError("a lowercase SHA-256 of the running test APK is required")
    if type(launch) is not bool:
        raise ValueError("launch must be an explicit bool")
    if type(fresh_install) is not bool:
        raise ValueError("fresh_install must be an explicit bool")
    for name, value in (
        ("expected_local_name", expected_local_name),
        ("service_filter", service_filter),
    ):
        if value is not None and (type(value) is not str or not value.strip()):
            raise ValueError(f"{name} must be a non-empty string or absent")
    if require_owner is not None and not callable(require_owner):
        raise TypeError("require_owner must be callable")
    if on_handoff is not None and not callable(on_handoff):
        raise TypeError("on_handoff must be callable")
    # Attach mode is the host-owned shape: the host installed and launched one process, and
    # the plan foregrounds it instead of starting a new one.
    attach = not launch
    available = plans(
        fresh_install=fresh_install,
        attach=attach,
        expected_local_name=expected_local_name,
        service_filter=service_filter,
    )
    if steps is not None:
        if type(plan_name) is not str or not plan_name.strip():
            raise ValueError("an explicit plan name is required")
        if type(steps) is not tuple and type(steps) is not list:
            raise ValueError("steps must be a list of plan steps")
        if not steps or any(type(step) is not dict for step in steps):
            raise ValueError("steps must be a non-empty list of step objects")
        chosen: tuple[dict[str, Any], ...] = tuple(steps)
    elif plan_name not in available:
        raise ValueError(
            f"unknown scene plan {plan_name!r}; known: {sorted(available)}"
        )
    else:
        chosen = available[plan_name]
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= TIMEOUT_MAX
    ):
        raise ValueError(f"timeout must be finite, positive and at most {TIMEOUT_MAX}")

    deadline = time.monotonic() + timeout
    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)
    directory = _plan_dir(run_id, plan_name)
    plan = {"launch": launch, "name": plan_name, "steps": list(chosen)}
    plan_path = attempt / "plan.json"
    publish_new_json(plan_path, plan)

    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "android",
        "device": device,
        "app_id": app_id,
        "activity": activity,
        "plan": plan_name,
        "steps": len(plan["steps"]),
        "timeout": timeout,
        "launch": launch,
        "attach": attach,
        "fresh_install": fresh_install,
        "expected_local_name": expected_local_name,
        "service_filter": service_filter,
        "test_component": TEST_COMPONENT,
        "test_class": TEST_CLASS,
        "test_apk_sha256": test_apk_sha256,
        "receipt": None,
        "hierarchy": None,
        "projection": None,
        "step_seconds_total": None,
        "instrumentation_returncode": None,
        "instrumentation_seconds": None,
        "instrumentation_lingered": None,
        "instrumentation_output": None,
        "handoffs": [],
        "markers": [],
        "status": "failed",
        "primary_error": None,
        "primary_traceback": None,
    }
    guard = require_owner if require_owner is not None else lambda: None
    primary: BaseException | None = None
    capture: MarkerCapture | None = None
    try:
        _clear_test_files(device, app_id, cwd=cwd)
        adb.run_command(
            adb.adb_command(device, "shell", "mkdir", "-p", directory),
            cwd=cwd,
            timeout=_remaining(deadline, "scratch directory"),
            check=True,
        )
        adb.run_command(
            adb.adb_command(device, "push", str(plan_path), f"{directory}/plan.json"),
            cwd=cwd,
            timeout=_remaining(deadline, "plan push"),
            check=True,
        )
        arguments = {
            "plan": f"{directory}/plan.json",
            "receipt": RECEIPT_FILE,
            "hierarchy": HIERARCHY_FILE,
            "done": DONE_FILE,
            "component": f"{app_id}/{activity}",
            "budgetMs": str(max(1, int(_remaining(deadline, "scene run") * 1000))),
        }
        capture = MarkerCapture(device, deadline, tag=LOG_TAG, host_tag=HOST_MARKER_TAG)
        try:
            summary = _drive_scene(
                device,
                arguments,
                capture=capture,
                app_id=app_id,
                deadline=deadline,
                guard=guard,
                on_handoff=on_handoff if on_handoff is not None else (lambda _: None),
                cwd=cwd,
                log=attempt / "run.log",
            )
        except BaseException as error:  # noqa: BLE001 -- the receipt still explains it
            primary = error
        else:
            # Recorded before its exit is judged: a non-passing exit must not erase the
            # returncode, markers and answered handoffs that explain it.
            result["instrumentation_returncode"] = summary["returncode"]
            result["instrumentation_seconds"] = round(summary["elapsed_seconds"], 3)
            result["instrumentation_lingered"] = summary["lingered"]
            result["handoffs"] = summary["handoffs"]
            result["markers"] = summary["markers"]
            primary = _exit_verdict(summary)
        # A failed step fails the JUnit test, so the receipt is recovered after the failure
        # rather than only on the success path: it is what names the step that stopped.
        receipt: dict[str, Any] | None = None
        try:
            receipt = _recover_receipt(device, app_id, cwd=cwd, deadline=deadline)
            require_receipt(
                receipt, scope=ANDROID_RECEIPT_SCOPE, steps=len(plan["steps"])
            )
        except BaseException as error:  # keep whatever the executor already reported
            if primary is None:
                raise
            error_text = "".join(traceback.format_exception_only(error)).strip()
            primary = RuntimeError(
                f"{''.join(traceback.format_exception_only(primary)).strip()}; "
                f"scene receipt: {error_text}"
            )
            primary.__traceback__ = None
            receipt = None
        if receipt is not None:
            result["receipt"] = receipt
            rows = receipt["steps"]
            result["step_seconds_total"] = round(
                sum(float(row.get("seconds") or 0.0) for row in rows), 3
            )
            result["projection"] = decode_projection(
                plan, receipt, partial=receipt.get("status") != "passed"
            )
            if receipt.get("status") != "passed":
                # The receipt names the step that stopped and what the App showed; the
                # executor's own non-passing exit only echoes that, so it is a note.
                step_failure = RuntimeError(
                    f"scene plan failed: {receipt.get('primary_error')}"
                )
                if primary is not None:
                    step_failure.add_note(
                        "executor: "
                        + "".join(traceback.format_exception_only(primary)).strip()
                    )
                primary = step_failure
        try:
            hierarchy = _test_file(
                device,
                app_id,
                HIERARCHY_FILE,
                timeout=_remaining(deadline, "hierarchy read"),
                cwd=cwd,
            )
        except (OSError, RuntimeError):
            hierarchy = b""
        # `adb` can report success while the remote `cat` failed, so the payload is checked
        # by shape rather than trusted: the dump is a diagnostic, not evidence.
        if hierarchy and not hierarchy.lstrip().startswith(b"<?xml"):
            (attempt / "hierarchy-error.txt").write_bytes(hierarchy[:2048])
            hierarchy = b""
        if hierarchy:
            (attempt / "hierarchy.xml").write_bytes(hierarchy[:MAX_HIERARCHY_BYTES])
            result["hierarchy"] = "hierarchy.xml"
        if primary is None:
            result["status"] = "passed"
    except BaseException as error:  # noqa: BLE001 -- publish the attempt, then propagate
        primary = primary or error
    finally:
        if capture is not None:
            capture.close()
        try:
            _remove_run_files(device, app_id, cwd=cwd)
        except Exception as cleanup_error:  # noqa: BLE001 -- keep a scene failure primary.
            if primary is None:
                primary = cleanup_error
                result["status"] = "failed"
            else:
                primary.add_note(f"scene file cleanup: {cleanup_error}")
        _remove_device_dir(device, directory, cwd=cwd)

    if primary is not None:
        result["primary_error"] = "".join(
            traceback.format_exception_only(primary)
        ).strip()
        result["primary_traceback"] = traceback.format_exception(primary)
        # The raw run directory is temporary, so a failure that the receipt could not
        # explain must keep the executor's own tail here or it is lost with it.
        try:
            result["instrumentation_output"] = (attempt / "run.log").read_text(
                encoding="utf-8", errors="replace"
            )[-16384:]
        except OSError:
            result["instrumentation_output"] = None
    publish_new_json(attempt / "result.json", result)
    if primary is not None:
        raise primary
    return attempt / "result.json"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--activity", required=True)
    parser.add_argument("--plan", choices=sorted(plans()), default="probe")
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / ".work/android-scene-driver"
    )
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument(
        "--launch",
        choices=("true", "false"),
        default="true",
        help=(
            "true starts the App for a standalone run; false attaches to the App the host "
            "already launched, and the plan foregrounds it"
        ),
    )
    parser.add_argument(
        "--fresh-install",
        choices=("true", "false"),
        default="false",
        help="the App was just installed: look for the platform's permission prompts",
    )
    parser.add_argument("--expected-local-name")
    parser.add_argument("--service-filter")
    parser.add_argument("--apk", type=Path, help="optional standalone App install")
    parser.add_argument("--apk-sha256")
    parser.add_argument(
        "--test-apk", type=Path, help="optional standalone test APK install"
    )
    parser.add_argument("--test-apk-sha256")
    parser.add_argument("--cwd", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)

    try:
        if args.test_apk_sha256 is None:
            raise ValueError(
                "the standalone entry needs --test-apk-sha256: it is the artifact "
                "identity the run records"
            )
        for label, apk, digest, test_only in (
            ("app", args.apk, args.apk_sha256, False),
            ("test", args.test_apk, args.test_apk_sha256, True),
        ):
            if apk is None:
                continue
            if digest is None:
                raise ValueError(f"--{label}-apk requires a reviewed SHA-256")
            with apk.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError(f"{label} APK does not match the declared SHA-256")
            install_log = Path(".work/logs/android-scene-driver-install.log")
            install_log.parent.mkdir(parents=True, exist_ok=True)
            with install_log.open("a", encoding="utf-8") as log:
                android_sdk.install_apk(
                    args.device,
                    apk.absolute(),
                    log=log,
                    test_only=test_only,
                    use_root=True,
                    cwd=args.cwd,
                )
        path = run_scene(
            device=args.device,
            plan_name=args.plan,
            output_root=args.output,
            timeout=args.timeout,
            launch=args.launch == "true",
            app_id=args.app_id,
            activity=args.activity,
            test_apk_sha256=args.test_apk_sha256,
            fresh_install=args.fresh_install == "true",
            expected_local_name=args.expected_local_name,
            service_filter=args.service_filter,
            cwd=args.cwd,
        )
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        print(f"android_scene_driver=failed error={error}", file=sys.stderr)
        return 2
    result = json.loads(path.read_text(encoding="utf-8"))
    rows = (result.get("receipt") or {}).get("steps") or []
    for row in rows:
        step = row.get("step") or {}
        print(
            "android_scene_step"
            f" index={row.get('index')}"
            f" op={step.get('op')}"
            f" id={step.get('id')}"
            f" seconds={float(row.get('seconds') or 0.0):.3f}"
            + (f" error={row['error']}" if row.get("error") else "")
            + (
                f" alert={row['observed']['alert']}"
                if "alert" in (row.get("observed") or {})
                else ""
            ),
            flush=True,
        )
    total = sum(float(row.get("seconds") or 0.0) for row in rows)
    print(
        f"android_scene_driver=passed plan={result['plan']} steps={len(rows)} "
        f"total={total:.2f}s"
    )
    print(f"android_scene_driver_result={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
