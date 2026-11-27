"""Release this repository's formal iOS App before a run admits the device.

The formal producer refuses takeover when any process of its bundle is already running,
which is the right rule: it must never adopt a process it cannot attribute. What it cannot
do is release its own left-over either, because a passing run keeps the App as evidence and
the platform can later restore that App as the last foreground application. The observed
result is a lane that needs a manual step between runs, and a scene sequence that stalls on
its second scene.

This owner performs that one step with the strongest attribution the device itself offers:
the currently installed application for *this bundle id*, from the installation directory
the device reports for it. It terminates at most one process whose executable is inside that
exact directory, verifies absence from the process inventory, and records what it did. It
never touches another bundle, never retries the termination, and never claims resource-zero,
radio state, permission state or a passing gate.

Two answers that look like "nothing to release" are kept apart. A booted iPhone always reports
installed Apps, so an *empty* inventory means the device has not answered: observed on the first
query after a new CoreDevice tunnel, `devicectl device info apps` returned `apps: []` with
`outcome: success` while this very App was installed and running. That answer is asked again a
bounded number of times and then refused. Only a device that did answer, and reports no
installation of this bundle, has nothing to release.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.react_native import ios_sdk
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease

DEFAULT_BUNDLE = "FWVYQ287W5.bleng-rn-host"
# The platform restores an App it terminated as the last foreground one, and it does so a
# couple of seconds after the previous scene's teardown. A single inventory therefore
# races with that restore: measured once, a release reported `absent` and the producer's
# admission saw the same installation running immediately afterwards. Absence is required
# across this many consecutive inventories, with at most one termination per round.
SETTLE_INVENTORIES = 2
MAX_TERMINATIONS = 3
SETTLE_SECONDS = 2.0
# How many times an empty installed-Apps answer is asked again before the device is refused.
APP_INVENTORY_ATTEMPTS = 3


class FormalAppCleanupError(RuntimeError):
    """The App could not be attributed, or did not leave after one termination."""


def installation_directory(apps: list[dict[str, Any]], bundle_id: str) -> str | None:
    """The one installation directory the device reports for this bundle, or `None`.

    `None` means the device answered and has no installation of this bundle, so there is
    nothing of this App that could be running. An empty inventory is refused instead: read as
    "nothing is installed" it would release nothing while this App is running.
    """
    if not apps:
        raise FormalAppCleanupError(
            "the device reported no installed Apps at all; it has not answered"
        )
    rows = [
        row
        for row in apps
        if isinstance(row, dict) and row.get("bundleIdentifier") == bundle_id
    ]
    if len(rows) > 1:
        raise FormalAppCleanupError(
            f"device reports {len(rows)} installations for {bundle_id}; refusing to guess"
        )
    if not rows:
        return None
    url = rows[0].get("url")
    if not isinstance(url, str) or not url.endswith(".app/"):
        raise FormalAppCleanupError("installed App has no installation directory")
    return url[:-1]


def processes_inside(processes: list[dict[str, Any]], directory: str) -> list[int]:
    """PIDs whose executable is inside this installation directory and nowhere else.

    `directory` is the `file://` URL the device reports; the inventory reports URLs of the
    same shape, so both are normalized to their filesystem path before comparison.
    """
    base = ios_sdk._identity_executable_path(directory, file_url=True)
    prefix = base + "/"
    found: list[int] = []
    for row in processes:
        if not isinstance(row, dict):
            raise FormalAppCleanupError("malformed process inventory")
        executable = row.get("executable")
        if not isinstance(executable, str) or not executable.startswith("file://"):
            continue
        path = ios_sdk._identity_executable_path(executable, file_url=True)
        if path.startswith(prefix) and "/" not in path[len(prefix) :]:
            pid = row.get("processIdentifier")
            if type(pid) is not int or pid <= 0:
                raise FormalAppCleanupError("malformed process inventory")
            found.append(pid)
    return found


def release_app(
    *,
    device: str,
    output: Path,
    deadline: float,
    bundle_id: str = DEFAULT_BUNDLE,
    run_command=ios_sdk.run_logged,
    run_processes=ios_sdk.devicectl_processes,
    run_apps=ios_sdk.devicectl_device_apps,
    terminate=ios_sdk.devicectl_terminate_process,
) -> dict[str, Any]:
    """Terminate this bundle's App if it is running; record either way."""
    if output.exists():
        raise FormalAppCleanupError(f"refusing an existing receipt directory: {output}")
    output.mkdir(parents=True, mode=0o700)
    lease = IosRadioLease(
        device,
        str(uuid4()),
        output / "cleanup-lease.json",
        scope=SCAN_OBSERVATION_SCOPE,
    )
    receipt: dict[str, Any] = {
        "scope": "formal-ios-app-cleanup",
        "device": device,
        "app_id": bundle_id,
        "installation": None,
        "action": "none",
        "pid": None,
        "terminations": 0,
        "status": "failed",
        "error": None,
        "detail": (
            "One bounded termination of this bundle's own installed App. Not "
            "resource-zero, radio state, permission state or a passing gate."
        ),
    }

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise FormalAppCleanupError("cleanup deadline exceeded")
        return min(30.0, left)

    def command(argv: list[str], name: str, kind: str) -> dict[str, Any]:
        lease.require_owner()
        path = output / f"{name}.json"
        if path.exists() or path.is_symlink():
            raise FormalAppCleanupError("refusing an existing cleanup receipt")
        run_command(
            argv, cwd=Path.cwd(), log=path.with_suffix(".log"), timeout=remaining()
        )
        lease.require_owner()
        return ios_sdk.official_result(path, kind, device)

    def inventory(name: str) -> list[dict[str, Any]]:
        rows = command(
            run_processes(device, output / f"{name}.json", timeout=5),
            name,
            "devicectl.device.info.processes",
        ).get("runningProcesses")
        if not isinstance(rows, list):
            raise FormalAppCleanupError("malformed process inventory")
        return rows

    lease.acquire()
    cleanup: list[str] = []
    try:
        apps: list[Any] = []
        for attempt in range(1, APP_INVENTORY_ATTEMPTS + 1):
            name = f"01-apps-{attempt:02d}"
            rows = command(
                run_apps(device, output / f"{name}.json", timeout=8),
                name,
                "devicectl.device.info.apps",
            ).get("apps")
            if not isinstance(rows, list):
                raise FormalAppCleanupError("malformed installed App inventory")
            if rows:
                apps = rows
                break
            # Success with no Apps at all is no answer; ask again rather than conclude.
            if attempt < APP_INVENTORY_ATTEMPTS:
                time.sleep(min(SETTLE_SECONDS, remaining()))
        directory = installation_directory(apps, bundle_id)
        receipt["installation"] = directory
        terminations = 0
        if directory is not None:
            clean_inventories = 0
            for round_number in range(1, MAX_TERMINATIONS + 1):
                found = processes_inside(
                    inventory(f"02-processes-{round_number:02d}"), directory
                )
                if len(found) > 1:
                    raise FormalAppCleanupError(
                        "several processes run this installation"
                    )
                if not found:
                    clean_inventories += 1
                    if clean_inventories >= SETTLE_INVENTORIES:
                        break
                    time.sleep(min(SETTLE_SECONDS, remaining()))
                    continue
                pid = found[0]
                receipt.update(pid=pid)
                command(
                    terminate(
                        device, pid, output / f"03-terminate-{round_number:02d}.json"
                    ),
                    f"03-terminate-{round_number:02d}",
                    "devicectl.device.process.terminate",
                )
                terminations += 1
                clean_inventories = 0
                time.sleep(min(SETTLE_SECONDS, remaining()))
            else:
                raise FormalAppCleanupError(
                    "App did not stay absent across consecutive inventories after "
                    f"{terminations} terminations"
                )
            receipt["action"] = "terminated" if terminations else "absent"
        else:
            receipt["action"] = "not-installed"
        receipt["terminations"] = terminations
        receipt["status"] = "passed"
    except BaseException as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        cleanup = lease.finish_observation(None, finalize_owned=lambda: None)
        receipt["lease_cleanup_errors"] = cleanup
        (output / "cleanup.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if cleanup:
        raise FormalAppCleanupError(f"cleanup lease did not release: {cleanup}")
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Release this bundle's App once, as an explicit operator action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        receipt = release_app(
            device=args.device,
            bundle_id=args.bundle_id,
            output=args.output.absolute(),
            deadline=time.monotonic() + args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"formal_ios_app_cleanup=failed error={error}", file=sys.stderr)
        return 2
    print(
        f"formal_ios_app_cleanup={receipt['status']} action={receipt['action']} "
        f"pid={receipt['pid']} installation={receipt['installation']} "
        f"receipt={args.output.absolute() / 'cleanup.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
