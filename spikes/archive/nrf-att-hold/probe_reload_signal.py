"""Isolated, non-RF iPhone smoke of host-triggered RN engine reload."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from run_dk_initial_read import APP, FIXTURE_PID, FIXTURE_WORK, LOCK, ROOT, WORK

PREFIX = "BLEHUB_NITRO_RUNTIME "
MARKERS = (
    "reload:notification-registered",
    "reload:ready",
    "reload:external-ready",
    "reload:notification-received",
    "reload:cleanup-started",
    "reload:ready",
    "reload:complete:passed",
)


def verify_markers(text: str) -> None:
    """Require ordered, unique old/new engine boundaries without a timer fallback."""
    lines = [line for line in text.splitlines() if line.startswith(PREFIX)]
    offsets: list[int] = []
    for marker in MARKERS:
        matching = [index for index, line in enumerate(lines) if line == PREFIX + marker]
        required = MARKERS.count(marker)
        if len(matching) != required:
            raise RuntimeError(f"expected {required} {marker}, found {len(matching)}")
        offsets.append(matching[MARKERS[: len(offsets)].count(marker)])
    if offsets != sorted(offsets) or len(set(offsets)) != len(offsets):
        raise RuntimeError("RN external reload boundaries are out of order")
    if any(line == PREFIX + "reload:native-triggered" for line in lines):
        raise RuntimeError("fixed-delay reload fired in the external-trigger experiment")
    if any(line == PREFIX + "reload:notification-before-ready" for line in lines):
        raise RuntimeError("reload notification arrived before the first engine was ready")
    if any("reload:failed:" in line or "reload:close-rejected:" in line for line in lines):
        raise RuntimeError("RN external reload reported failure")


def wait_for_marker(log: Path, marker: str, process: subprocess.Popen[str], timeout: float) -> None:
    """Observe one app marker within a monotonic bound, failing on early exit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        if PREFIX + marker in output:
            return
        if process.poll() is not None:
            raise RuntimeError(f"RN console exited before {marker}: {log}")
        time.sleep(0.05)
    raise TimeoutError(f"RN console did not report {marker} in {timeout}s: {log}")


def run(udid: str) -> None:
    """Install the existing signed app and post one per-attempt native reload signal."""
    if not APP.is_dir():
        raise RuntimeError(f"signed iPhone app missing: {APP}")
    if FIXTURE_PID.exists():
        raise RuntimeError(f"refusing to overlap existing fixture: {FIXTURE_PID}")
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise RuntimeError("xcrun is unavailable")
    identity = uuid.uuid4().hex
    name = f"dev.blehub.rn.reload.{uuid.UUID(hex=identity)}"
    WORK.mkdir(parents=True, exist_ok=True)
    FIXTURE_WORK.mkdir(parents=True, exist_ok=True)
    LOCK.mkdir()
    process: subprocess.Popen[str] | None = None
    try:
        result = subprocess.run(
            [
                xcrun,
                "devicectl",
                "device",
                "info",
                "lockState",
                "--device",
                udid,
                "--timeout",
                "15",
                "--json-output",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        state = json.loads(result.stdout)["result"]
        if state["passcodeRequired"] or not state["unlockedSinceBoot"]:
            raise RuntimeError("unlock the iPhone before the non-RF reload smoke")
        subprocess.run(
            [xcrun, "devicectl", "device", "install", "app", "--device", udid, str(APP)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        log = WORK / f"rn-reload-signal-{identity}.attempt.log"
        console = WORK / f"rn-reload-signal-{identity}.runner.log"
        print(f"reload_signal_attempt_log={log}", flush=True)
        with console.open("x", encoding="utf-8") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tools/react_native/run_ios_device_runtime.py"),
                    udid,
                    str(log),
                    "--seconds",
                    "55",
                    "--reload",
                    "--reload-notification-name",
                    name,
                ],
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
            )
            wait_for_marker(log, "reload:external-ready", process, 35)
            started = time.monotonic()
            subprocess.run(
                [
                    xcrun,
                    "devicectl",
                    "device",
                    "notification",
                    "post",
                    "--device",
                    udid,
                    "--name",
                    name,
                    "--timeout",
                    "12",
                    "--json-output",
                    "-",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=18,
            )
            wait_for_marker(log, "reload:notification-received", process, 5)
            response_seconds = time.monotonic() - started
            process.wait(timeout=65)
        verify_markers(log.read_text(encoding="utf-8"))
        if process.returncode != 0:
            raise RuntimeError(f"RN console exited {process.returncode}: {console}")
        print(f"reload_signal_response_seconds={response_seconds:.3f}")
        print(f"reload_signal_non_rf=passed:log={log}")
    finally:
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=70)
            except subprocess.TimeoutExpired as error:
                print(f"runtime still live: retaining {LOCK}; pid={process.pid}", file=sys.stderr)
                raise RuntimeError("RN console did not exit within bounded cleanup") from error
        LOCK.rmdir()


def main() -> int:
    """Validate explicit device identity and run the isolated host trigger probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udid", required=True)
    args = parser.parse_args()
    try:
        if not args.udid or len(args.udid) != 25:
            raise ValueError("--udid must identify the intended physical iPhone")
        run(args.udid)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"RN reload-signal smoke failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
