"""Android RN host installation identity and bounded first-process entry."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from tests.peripherals.nrf52840.cancellation_control import write_document
from tools.android_toolchain import PACKAGES, sdk_root
from tools.react_native import android_sdk
from tools.react_native.host_contract import ANDROID_ACTIVITY, ANDROID_PACKAGE

INITIAL_PID_TIMEOUT_SECONDS = 4.0
INITIAL_PID_POLL_SECONDS = 0.1


class AndroidRfHostError(RuntimeError):
    """Raised when installation or initial process identity cannot be verified."""


def verify_install_identity(apk: Path, attempt: Path) -> None:
    """Reject a candidate that would install one package but launch another."""
    tools = next(
        package for package in PACKAGES if package.package.startswith("build-tools/")
    )
    aapt2 = sdk_root() / tools.directory / tools.required_files[0]
    result = android_sdk.run_command(
        [str(aapt2), "dump", "badging", str(apk)], timeout=30, check=True
    )
    (attempt / "artifact-badging.log").write_text(result.stdout, encoding="utf-8")
    packages = re.findall(r"^package: name='([^']+)'", result.stdout, re.MULTILINE)
    activities = re.findall(
        r"^launchable-activity: name='([^']+)'", result.stdout, re.MULTILINE
    )
    matched = packages == [ANDROID_PACKAGE] and activities == [ANDROID_ACTIVITY]
    write_document(
        attempt / "artifact-install-identity.json",
        {
            "schema_version": 1,
            "status": "passed" if matched else "failed",
            "apk": apk.relative_to(attempt).as_posix(),
            "method": "pinned-sdk-aapt2-dump-badging",
            "package_names": packages,
            "launchable_activities": activities,
            "expected_package": ANDROID_PACKAGE,
            "expected_activity": ANDROID_ACTIVITY,
        },
    )
    if not matched:
        raise AndroidRfHostError(
            "frozen APK package/launchable activity does not match the RN host"
        )


def wait_for_initial_pid(
    serial: str,
    log_path: Path,
    timeout_seconds: float,
    *,
    guard: Callable[[], None],
) -> str:
    """Wait only for first process entry; never reselect an already pinned PID."""
    started = time.monotonic()
    budget = min(INITIAL_PID_TIMEOUT_SECONDS, timeout_seconds)
    deadline = started + budget
    observations: list[dict[str, object]] = []
    identity: dict[str, object] = {
        "schema_version": 1,
        "device_serial": serial,
        "package_name": ANDROID_PACKAGE,
        "boot_budget_seconds": budget,
        "status": "booting",
        "pid": None,
        "observations": observations,
    }
    path = log_path.with_suffix(".identity.json")
    try:
        while True:
            guard()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AndroidRfHostError(
                    "Android RN initial PID boot deadline exceeded"
                )
            result = android_sdk.run_command(
                android_sdk.adb_command(serial, "shell", "pidof", ANDROID_PACKAGE),
                timeout=min(1.0, remaining),
                check=False,
            )
            elapsed = time.monotonic() - started
            output = result.stdout.strip()
            observations.append(
                {
                    "elapsed_seconds": elapsed,
                    "exit_code": result.returncode,
                    "output": output,
                }
            )
            write_document(path, identity)
            guard()
            if time.monotonic() >= deadline:
                raise AndroidRfHostError(
                    "Android RN initial PID boot deadline exceeded"
                )
            if result.returncode == 0:
                if not output.isdecimal() or int(output) <= 0:
                    raise AndroidRfHostError(
                        "expected one positive initial Android RN PID"
                    )
                identity.update(
                    status="pinned", pid=output, boot_elapsed_seconds=elapsed
                )
                write_document(path, identity)
                return output
            if result.returncode != 1 or output:
                raise AndroidRfHostError(
                    f"initial Android pidof failed: exit={result.returncode}; {output}"
                )
            time.sleep(
                min(INITIAL_PID_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
            )
    except BaseException as error:
        identity.update(status="failed", primary_error=str(error))
        try:
            write_document(path, identity)
        except (OSError, ValueError, RuntimeError) as publication_error:
            error.add_note(
                f"initial PID evidence publication failed: {publication_error}"
            )
        raise
