"""Drive one formal iOS UI scene in-process with XCTest instead of WebDriver.

Why this exists: the Appium lane spends most of its time in XCUITest's accessibility
snapshot, once per element lookup, over an HTTP hop through WebDriverAgent. This lane
runs the same identifiers through `XCUIElement` in the test process: no WDA, no Appium
server, no tunnel, no element-id round trips.

What does **not** move here: the fixture, the physical phone lease, scene markers,
evidence publication and the verdict. Those stay host-owned. This entry only drives the
UI and returns timings/observations the host can audit.

It neither builds nor signs, and it never installs: it consumes the signed App and
manifest `formal-host-ios-device-test-build` produced, and launches whatever App that
build installed. A scene that needs the fixture requires the host to have started it.

Two contracts this module owns:

* **Plans are declarations, not procedures.** A step is `wait`, `tap`, `tapWhen`,
  `tapUntil` or `read`, and every one of them names an identifier from
  `tests/consumers/react-native-bleng/src/ids.ts`. The driver cannot scroll, so a plan
  that needs an element the accessibility tree does not have is a bug in the App's pinned
  machine surface, not something to work around with a gesture.
* **The receipt is an attachment.** The UI-test process's stdout does not reach the
  `xcodebuild` log, so the driver publishes one `XCTAttachment` and this module reads it
  back with `xcrun xcresulttool export attachments`. No stdout scraping, no device IPC.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.file_digest import sha256_file

from . import ios_sdk
from .bleng_ios_native_common import resolve_xctestrun, staged_xctestrun
from .capture_io import publish_new_json
from .host_paths import PROJECT_ROOT
from .scene_receipt import IOS_RECEIPT_SCOPE, decode_projection, require_receipt

SCOPE = "formal-ios-scene-driver"
TEST_BUNDLE = "BleHubBlengHostUITests.xctest"
TEST_CLASS = "BlengSceneDriverUITests"
METHOD = "testScenePlan"
PLAN_VARIABLE = "BLEHUB_SCENE_PLAN"
# Must equal `BlengSceneDriverUITests.receiptName`; the host reads it back by this name.
RECEIPT_NAME = "ios-scene-driver-receipt"
TIMEOUT_MAX = 1800
XCRUN = "/usr/bin/xcrun"
RUNNER_AUTOMATION_TIMEOUT = (
    "The test runner failed to initialize for UI testing. "
    "(Underlying Error: Timed out while enabling automation mode.)"
)
# How much of `xcodebuild`'s output a failed result keeps.
MAX_OUTPUT_TAIL = 16384
# A phase whose steps took this long is not a normal scene (fast phases are 13-35 s in total),
# and its XCTest console tail is the only place the framework says *why* — "App animations
# complete notification not received", retries, and event-synthesis warnings are warnings in
# that log. A normal phase keeps nothing, so an ordinary bundle stays small.
SLOW_PHASE_SECONDS = 120.0
_SAFE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# `xcresulttool` stores an attachment's declared name with a uniqueness suffix
# (`<name>_<index>_<uuid>.<ext>`), so the receipt is matched by that exact shape, not by
# substring — a differently named attachment in the same bundle must never be mistaken for it.
_RECEIPT_FILE = re.compile(re.escape(RECEIPT_NAME) + r"_[0-9]+_[0-9A-Fa-f-]+\.json")


@dataclass(frozen=True)
class ExportedAttachments:
    """What one `xcresulttool export attachments` left on disk.

    The three views are kept apart because they are used at different moments: the
    diagnostics are published whatever happens next, the receipts are what turns into the
    one receipt, and the declared names are how a refusal says what it saw instead.
    """

    diagnostics: tuple[dict[str, Any], ...]
    receipts: tuple[str, ...]
    declared: tuple[str, ...]


# The plan vocabulary is shared with the Android lane; this module re-exports it so a caller
# that is only interested in "the iOS executor of the shared plans" needs one import.
from .scene_plans import (  # noqa: F401 -- re-exported for this lane's callers
    CANCELLATION_READS,
    CANCELLATION_TIMEOUT_MS,
    CONTROLS_REQUIRING_LINK,
    DEFAULT_OBSERVATION_TIMEOUT_MS,
    ENVIRONMENTAL_ALERTS,
    PASSIVE_SILENCE_SECONDS,
    PLANS,
    PROBE_PLAN,
    SCAN_PLAN,
    SMOKE_PLAN,
    STATUS_ID,
    VALUE_RENDERING,
    WRITE_REJECT_READS,
    WRITE_RENDERING,
    plans,
)


def _remaining(deadline: float, label: str) -> float:
    left = deadline - time.monotonic()
    if not math.isfinite(left) or left <= 0:
        raise RuntimeError(f"scene driver deadline expired before {label}")
    return left


def runner_initialization_error(log: str, *, returncode: int) -> RuntimeError | None:
    """Surface the observed XCTest failure before attempting to read a scene receipt."""
    if RUNNER_AUTOMATION_TIMEOUT not in log:
        return None
    return RuntimeError(
        f'XCTest reported "{RUNNER_AUTOMATION_TIMEOUT}" (xcodebuild exit={returncode}). '
        "The cause is unknown. Inspect xcodebuild_output in the driver result and the "
        "XCTest diagnostics in scene.xcresult if retained."
    )


def _export_attachments(
    bundle: Path, destination: Path, *, cwd: Path, timeout: float
) -> ExportedAttachments:
    """Export every attachment and split it into the receipt and the diagnostics.

    `export attachments` writes every attachment plus a `manifest.json` that names them.
    The manifest is the only trustworthy mapping from an attachment's declared name to its
    exported file, so the receipt is looked up by name rather than by "the only .json".

    This stops one step short of demanding a receipt on purpose. A run whose export
    succeeded but whose receipt is missing, ambiguous or foreign is exactly the failure
    whose screen record is worth keeping, so the listing is built first and returned even
    when `_read_receipt` refuses the result.
    """
    completed = ios_sdk.run_command(
        [
            XCRUN,
            "xcresulttool",
            "export",
            "attachments",
            "--path",
            str(bundle),
            "--output-path",
            str(destination),
        ],
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            "could not export scene attachments "
            f"(exit={completed.returncode}): {(completed.stdout or '')[-800:]}"
        )
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(
            f"attachment export produced no manifest at {manifest_path}: "
            f"{(completed.stdout or '')[-800:]}"
        )
    groups = json.loads(manifest_path.read_text(encoding="utf-8"))
    if type(groups) is not list:
        raise RuntimeError("attachment manifest is not a list of test groups")
    diagnostics: list[dict[str, Any]] = []
    receipts: list[str] = []
    declared: list[str] = []
    for group in groups:
        entries = (group or {}).get("attachments") if isinstance(group, dict) else None
        if type(entries) is not list:
            continue
        for entry in entries:
            if type(entry) is not dict:
                continue
            name = entry.get("exportedFileName")
            if type(name) is not str or not _SAFE_FILE.fullmatch(name):
                raise RuntimeError(f"unsafe exported attachment name: {name!r}")
            suggested = str(entry.get("suggestedHumanReadableName"))
            declared.append(suggested)
            if _RECEIPT_FILE.fullmatch(suggested):
                receipts.append(name)
                continue
            diagnostics.append(
                {
                    "name": suggested,
                    "file": name,
                    "bytes": (destination / name).stat().st_size,
                }
            )
    return ExportedAttachments(tuple(diagnostics), tuple(receipts), tuple(declared))


def _read_receipt(destination: Path, export: ExportedAttachments) -> dict[str, Any]:
    """The one receipt attachment this lane publishes, or a refusal naming what it saw."""
    if len(export.receipts) != 1:
        raise RuntimeError(
            f"expected exactly one {RECEIPT_NAME!r} attachment, found {len(export.receipts)}; "
            f"exported names: {list(export.declared[:8])!r}"
        )
    payload = json.loads((destination / export.receipts[0]).read_bytes())
    if type(payload) is not dict or payload.get("scope") != IOS_RECEIPT_SCOPE:
        raise RuntimeError("scene receipt is not this driver's scope")
    return payload


def run_scene(
    *,
    device: str,
    xctestrun: Path,
    plan_name: str,
    output_root: Path,
    timeout: float,
    launch: bool,
    fresh_install: bool = False,
    expected_local_name: str | None = None,
    service_filter: str | None = None,
    steps: Sequence[dict[str, Any]] | None = None,
    cwd: Path,
    run_id: str | None = None,
) -> Path:
    """Drive one plan; publish a result and return its path.

    Nothing here is a gate verdict: the result reports the driver's own observations and
    per-step timings so the host can compare them with what the scene required.
    """
    if type(device) is not str or not device.strip():
        raise ValueError("an explicit physical device UDID is required")
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
    # Attach mode is the host-owned shape: the host installed, launched and pinned one
    # pid, so the plan foregrounds that process instead of starting a new one.
    attach = not launch
    available = plans(
        fresh_install=fresh_install,
        attach=attach,
        expected_local_name=expected_local_name,
        service_filter=service_filter,
    )
    if steps is not None:
        # A managed scene may be a phase list, and its steps come from the driver module
        # that composed them. `plan_name` stays the identity the receipt is published under.
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
    if not xctestrun.is_file() or xctestrun.is_symlink():
        raise ValueError(f"missing xctestrun: {xctestrun}")

    target = resolve_xctestrun(xctestrun, test_bundle=TEST_BUNDLE)
    deadline = time.monotonic() + timeout
    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)

    plan = {
        "launch": launch,
        "steps": list(chosen),
    }
    result: dict[str, Any] = {
        "schema_version": 2,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "ios",
        "device": device,
        "plan": plan_name,
        "steps": len(plan["steps"]),
        "launch": launch,
        "attach": attach,
        "fresh_install": fresh_install,
        "expected_local_name": expected_local_name,
        "service_filter": service_filter,
        "xctestrun_sha256": sha256_file(xctestrun),
        "test_target": target.name,
        "receipt": None,
        "attachments": [],
        "projection": None,
        "step_seconds_total": None,
        "xcodebuild_returncode": None,
        "xcodebuild_output": None,
        "status": "failed",
        "primary_error": None,
        "primary_traceback": None,
    }
    primary: BaseException | None = None
    try:
        staged = staged_xctestrun(
            xctestrun,
            attempt / "scene.xctestrun",
            test_bundle=TEST_BUNDLE,
            environment={PLAN_VARIABLE: json.dumps(plan, sort_keys=True)},
        )
        bundle = attempt / "scene.xcresult"
        command = ios_sdk.xcodebuild_test_without_building(
            xctestrun=staged,
            device=device,
            result=bundle,
            only_testing=f"{target.name}/{TEST_CLASS}/{METHOD}",
            quiet=True,
        )
        completed = ios_sdk.run_command(
            command,
            cwd=cwd,
            timeout=_remaining(deadline, "scene run"),
            check=False,
        )
        log = completed.stdout or ""
        (attempt / "xcodebuild.log").write_text(log, encoding="utf-8")
        result["xcodebuild_returncode"] = completed.returncode
        result["result_bundle"] = str(bundle)
        init_error = runner_initialization_error(log, returncode=completed.returncode)
        if init_error is not None:
            raise init_error
        if not bundle.is_dir():
            raise RuntimeError(
                "xcodebuild produced no result bundle "
                f"(exit={completed.returncode}): {log[-1200:]}"
            )
        export = _export_attachments(
            bundle,
            attempt / "attachments",
            cwd=cwd,
            timeout=_remaining(deadline, "attachment export"),
        )
        # The listing is published before the receipt is demanded: if the receipt is
        # missing or ambiguous, what XCTest did attach still reaches the bundle instead of
        # dying with the attempt directory.
        result["attachments"] = [dict(row) for row in export.diagnostics]
        receipt = _read_receipt(attempt / "attachments", export)
        result["receipt"] = receipt
        # A receipt is this lane's own report; the exit status is XCTest's. They must
        # agree: a hard XCTest failure inside a step (an ambiguous query, a vanished
        # element) aborts the test without throwing, so the receipt alone could describe
        # a scene that never ran to the end.
        if completed.returncode:
            raise RuntimeError(
                f"xcodebuild failed (exit={completed.returncode}) "
                f"although the receipt says {receipt.get('status')!r}: {log[-1200:]}"
            )
        steps = require_receipt(
            receipt, scope=IOS_RECEIPT_SCOPE, steps=len(plan["steps"])
        )["steps"]
        result["step_seconds_total"] = round(
            sum(float(step.get("seconds") or 0.0) for step in steps), 3
        )
        if result["step_seconds_total"] >= SLOW_PHASE_SECONDS:
            # Measured on 2026-11-26: single presses cost ~121 s while `hittable` stayed at
            # 0.1 s, and the receipt could only name the cost. This keeps XCTest's own words
            # for that shape without carrying a console tail on every ordinary run.
            result["xcodebuild_output"] = log[-MAX_OUTPUT_TAIL:]
        result["projection"] = decode_projection(
            plan, receipt, partial=receipt.get("status") != "passed"
        )
        if receipt.get("status") != "passed":
            raise RuntimeError(f"scene plan failed: {receipt.get('primary_error')}")
        result["status"] = "passed"
    except BaseException as error:  # noqa: BLE001 - publish the attempt before propagating
        primary = error
        result["primary_error"] = "".join(
            traceback.format_exception_only(error)
        ).strip()
        result["primary_traceback"] = traceback.format_exception(error)
        # A managed scene runs this lane in a directory it discards, so XCTest's own words
        # for a failure travel with the result; "no receipt" alone says nothing about why.
        try:
            result["xcodebuild_output"] = (attempt / "xcodebuild.log").read_text(
                encoding="utf-8", errors="replace"
            )[-MAX_OUTPUT_TAIL:]
        except OSError:
            result["xcodebuild_output"] = None

    publish_new_json(attempt / "result.json", result)
    if primary is not None:
        raise primary
    return attempt / "result.json"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--plan", choices=sorted(PLANS), default="smoke")
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / ".work/ios-scene-driver"
    )
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument(
        "--launch",
        choices=("true", "false"),
        default="true",
        help=(
            "true starts a fresh App process (standalone use); false attaches to the App "
            "the host already launched and pinned, and the plan foregrounds it"
        ),
    )
    parser.add_argument(
        "--fresh-install",
        choices=("true", "false"),
        default="false",
        help=(
            "the App was just installed: also answer the local-network and Bluetooth "
            "system prompts, each only when its declared text is actually shown"
        ),
    )
    parser.add_argument(
        "--expected-local-name",
        help=(
            "the peer this scan scene is looking for; without it any nearby "
            "advertisement satisfies the scene, which proves nothing about the DUT"
        ),
    )
    parser.add_argument(
        "--service-filter",
        help="canonical service UUID the App must adopt before scanning",
    )
    parser.add_argument("--cwd", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)

    try:
        path = run_scene(
            device=args.device,
            xctestrun=args.xctestrun,
            plan_name=args.plan,
            output_root=args.output,
            timeout=args.timeout,
            launch=args.launch == "true",
            fresh_install=args.fresh_install == "true",
            expected_local_name=args.expected_local_name,
            service_filter=args.service_filter,
            cwd=args.cwd,
        )
    except (ValueError, RuntimeError) as error:
        print(f"ios_scene_driver=failed error={error}", file=sys.stderr)
        return 2
    result = json.loads(path.read_text(encoding="utf-8"))
    steps = (result.get("receipt") or {}).get("steps") or []
    for step in steps:
        print(
            "ios_scene_step"
            f" index={step.get('index')}"
            f" op={(step.get('step') or {}).get('op')}"
            f" id={(step.get('step') or {}).get('id')}"
            f" seconds={float(step.get('seconds') or 0.0):.3f}"
            + (f" error={step.get('error')}" if step.get("error") else "")
            + (
                f" alert={step['observed']['alert']}"
                if "alert" in (step.get("observed") or {})
                else ""
            ),
            flush=True,
        )
    total = sum(float(step.get("seconds") or 0.0) for step in steps)
    print(
        f"ios_scene_driver=passed plan={result['plan']} steps={len(steps)} total={total:.2f}s"
    )
    print(f"ios_scene_driver_result={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
