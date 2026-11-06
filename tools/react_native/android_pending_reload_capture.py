"""Android pending-reload logcat capture and PID-pinned native controls."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fixture_session import read_live_document
from tests.peripherals.nrf52840.pending_reload import (
    PHYSICAL_NAME,
    PROGRESS_NAME,
    RuntimeFacts,
)
from tools.react_native import android_sdk
from tools.react_native.android_rf_host import wait_for_initial_pid
from tools.react_native.host_contract import ANDROID_ACTIVITY, ANDROID_PACKAGE
from tools.react_native.rf_control_relay import android_pid

PREFIX = "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:"


def _normalized(raw: bytes, pid: str) -> str:
    """Retain complete records and duplicates from the pinned logcat source."""
    result = []
    complete = raw[: raw.rfind(b"\n") + 1].decode("utf-8", errors="strict")
    for line in complete.splitlines():
        if PREFIX not in line:
            continue
        header = re.match(r"\S+\s+\S+\s+(\d+)\s+", line)
        if header is None or header[1] != pid:
            raise RuntimeError("foreign PID pending-reload marker")
        result.append(PREFIX + line.split(PREFIX, 1)[1])
    return "".join(line + "\n" for line in result)


def _send(serial: str, pid: str, arguments: list[str], guard: Any) -> None:
    guard()
    if android_pid(serial) != pid:
        raise RuntimeError("pending reload process identity changed before control")
    result = android_sdk.run_command(
        android_sdk.adb_command(serial, *arguments), timeout=3, check=True
    )
    delivered = "Warning: Activity not started, intent has been delivered to currently running top-most instance."
    if re.search(
        r"(?:Error:|Exception|Activity not started)",
        result.stdout.replace(delivered, ""),
    ):
        raise RuntimeError(f"Android pending control rejected: {result.stdout[-1000:]}")
    guard()
    if android_pid(serial) != pid:
        raise RuntimeError("pending reload control changed the App process")


def capture(
    serial: str,
    log_path: Path,
    coordination: Path,
    *,
    run_id: str,
    seconds: float,
    guard: Any,
) -> tuple[str, tuple[str, ...]]:
    deadline = time.monotonic() + seconds
    pid = wait_for_initial_pid(serial, log_path, seconds, guard=guard)
    raw_path = log_path.with_suffix(".raw.log")
    capture_path = log_path.with_suffix(".capture.json")
    command = android_sdk.adb_command(
        serial, "logcat", "-b", "main", "--pid", pid, "-v", "threadtime"
    )
    actions: list[str] = []
    facts = RuntimeFacts(run_id)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "runtime_pid": pid,
        "command": command,
        "raw_log": raw_path.name,
        "run_id": run_id,
        "host_actions": actions,
        "status": "capturing",
    }
    previous = b""
    latest = ""
    next_guard = 0.0
    with raw_path.open("wb") as output:
        capture = subprocess.Popen(command, stdout=output, stderr=subprocess.DEVNULL)
        evidence["capture_pid"] = capture.pid
        capture_primary: BaseException | None = None
        try:
            write_document(capture_path, evidence)
            while time.monotonic() < deadline:
                if capture.poll() is not None:
                    raise RuntimeError("owned pending reload logcat capture exited")
                raw = raw_path.read_bytes()
                if len(raw) > 8 * 1024 * 1024 or not raw.startswith(previous):
                    raise RuntimeError("pending raw log overflow or prefix drift")
                previous = raw
                text = _normalized(raw, pid)
                if text != latest:
                    log_path.write_text(text, encoding="utf-8")
                    temporary = coordination / "runtime.log.tmp"
                    temporary.write_text(text, encoding="utf-8")
                    temporary.replace(coordination / "runtime.log")
                    facts.refresh(coordination / "runtime.log")
                    latest = text
                now = time.monotonic()
                if now >= next_guard:
                    guard()
                    if android_pid(serial) != pid:
                        raise RuntimeError("RN engine reload replaced its process")
                    next_guard = now + 0.5
                progress_path = coordination / PROGRESS_NAME
                if progress_path.exists():
                    progress = read_live_document(progress_path)
                    if progress.get("run_id") != run_id:
                        raise RuntimeError("foreign physical progress run ID")
                    if progress.get("status") == "failed":
                        raise RuntimeError("physical pending reload coordinator failed")
                    phase = progress.get("phase")
                    expected = (
                        ("armed-1", "arm-1"),
                        ("waiting-1", "reload"),
                        ("armed-2", "arm-2"),
                    )
                    if len(actions) < len(expected):
                        wanted_phase, action = expected[len(actions)]
                        if phase == wanted_phase:
                            if action == "reload":
                                arguments = [
                                    "shell",
                                    "am",
                                    "start",
                                    "-n",
                                    f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}",
                                    "--ez",
                                    "blehub.triggerReload",
                                    "true",
                                    "--es",
                                    "blehub.rfPendingReadRunId",
                                    run_id,
                                ]
                            else:
                                url = f"blehub-rn-spike://arm/{run_id}"
                                if progress.get("arm_url") != url:
                                    raise RuntimeError("foreign arm URL")
                                arguments = [
                                    "shell",
                                    "am",
                                    "start",
                                    "-n",
                                    f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}",
                                    "-a",
                                    "android.intent.action.VIEW",
                                    "-d",
                                    url,
                                ]
                            _send(serial, pid, arguments, guard)
                            actions.append(action)
                            write_document(capture_path, evidence)
                            print(
                                f"android-pending-reload:control:{action}:confirmed",
                                flush=True,
                            )
                physical = coordination / PHYSICAL_NAME
                if physical.exists():
                    if read_document(physical).get("status") != "passed":
                        raise RuntimeError("physical pending reload proof failed")
                    if "passed" in facts.seen:
                        if actions != ["arm-1", "reload", "arm-2"]:
                            raise RuntimeError("pending reload host actions incomplete")
                        guard()
                        if android_pid(serial) != pid:
                            raise RuntimeError(
                                "pending runtime PID changed at completion"
                            )
                        evidence["status"] = "passed"
                        write_document(capture_path, evidence)
                        return text, tuple(actions)
                time.sleep(0.05)
            raise TimeoutError(
                "Android two-engine pending reload exceeded host deadline"
            )
        except BaseException as error:
            capture_primary = error
            evidence.update(status="failed", primary_error=str(error))
            try:
                write_document(capture_path, evidence)
            except (OSError, ValueError, RuntimeError) as publication:
                error.add_note(f"capture publication failed: {publication}")
            raise
        finally:
            try:
                capture.terminate()
                try:
                    capture.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    capture.kill()
                    capture.wait(timeout=3)
            except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup:
                if capture_primary is not None:
                    capture_primary.add_note(f"logcat cleanup failed: {cleanup}")
                else:
                    raise
