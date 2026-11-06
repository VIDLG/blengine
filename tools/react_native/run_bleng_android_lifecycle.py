"""Managed Android lifecycle or idle/start-hold controls; no build, BLE grant or RF."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from .bleng_android_automation import HELPER_PACKAGES
from .bleng_android_lifecycle import run_android_lifecycle
from .bleng_lifecycle_evidence import (
    IDLE_CONTROL_SCOPE,
    SCOPE,
    START_HOLD_CONTROL_SCOPE,
    verify_lifecycle_bundle,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--activity", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--apk-sha256", required=True)
    parser.add_argument("--appium-home", type=Path, required=True)
    parser.add_argument("--appium-entry", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--appium-port", type=int, required=True)
    parser.add_argument("--system-port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--phase-timeout", type=float, default=30)
    parser.add_argument("--cleanup-seconds", type=float, default=60)
    parser.add_argument(
        "--helper-sha256",
        action="append",
        required=True,
        metavar="PACKAGE=SHA256",
        help="repeat once for each of the three reviewed Appium helper APKs",
    )
    parser.add_argument("--authorize-install", action="store_true", required=True)
    parser.add_argument(
        "--authorize-helper-settings", action="store_true", required=True
    )
    parser.add_argument(
        "--use-root",
        action="store_true",
        help="explicit root App installer opt-in; no fallback",
    )
    parser.add_argument(
        "--close-diagnostics",
        action="store_true",
        help="explicitly enable local Appium debug/timestamps and a pre-Start fixed-tag live logcat stream (scenario deadline/300s, 8 MiB, bounded host arrivals); probe bound UiAutomator2 status and existing-daemon read-only ADB stages once on UI failure before DELETE (ADB at most 3s, no autostart/kill), reap only this capture child, capture original server/App PID logs, and restore the fixed native Close log tag under the phone lease; logs may contain sensitive system/UI data",
    )
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument(
        "--automation-idle-control",
        action="store_true",
        help="diagnostic control only: browse idle UI without Start/Close or BLE; requires --close-diagnostics and uses a distinct evidence scope, never a lifecycle pass",
    )
    controls.add_argument(
        "--automation-start-hold-control",
        action="store_true",
        help="diagnostic control only: Start once, verify one snapshot, hold Current run quietly for five bounded seconds, then Close on the same page; no navigation/scan; requires --close-diagnostics and never claims a lifecycle/host-gate pass",
    )
    args = parser.parse_args(argv)
    if (
        args.automation_idle_control or args.automation_start_hold_control
    ) and not args.close_diagnostics:
        parser.error("automation controls require --close-diagnostics")
    if args.automation_start_hold_control:
        result_key = "formal_android_managed_start_hold_control"
        expected_scope = START_HOLD_CONTROL_SCOPE
    elif args.automation_idle_control:
        result_key = "formal_android_managed_idle_control"
        expected_scope = IDLE_CONTROL_SCOPE
    else:
        result_key = "formal_android_managed_lifecycle"
        expected_scope = SCOPE
    helpers: dict[str, str] = {}
    for item in args.helper_sha256:
        package, separator, digest = item.partition("=")
        if (
            not separator
            or package not in HELPER_PACKAGES
            or package in helpers
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            parser.error(
                "each helper must have one unique known PACKAGE=lowercase-SHA256"
            )
        helpers[package] = digest
    if set(helpers) != set(HELPER_PACKAGES):
        parser.error("exactly three reviewed helper SHA256 values required")
    try:
        result_path = run_android_lifecycle(
            device=args.device,
            app_id=args.app_id,
            activity=args.activity,
            # Do not resolve away symlinks: the runner must reject them itself.
            apk=args.apk.absolute(),
            apk_sha256=args.apk_sha256,
            appium_home=args.appium_home.absolute(),
            appium_entry=args.appium_entry.absolute(),
            node=args.node,
            expected_helper_sha256=helpers,
            appium_port=args.appium_port,
            system_port=args.system_port,
            output_root=args.output.absolute(),
            timeout=args.timeout,
            phase_timeout=args.phase_timeout,
            cleanup_seconds=args.cleanup_seconds,
            authorize_install=args.authorize_install,
            authorize_helper_settings=args.authorize_helper_settings,
            use_root=args.use_root,
            cwd=PROJECT_ROOT,
            close_diagnostics=args.close_diagnostics,
            automation_idle_control=args.automation_idle_control,
            automation_start_hold_control=args.automation_start_hold_control,
        )
        result = verify_lifecycle_bundle(result_path.parent)
        if result["scope"] != expected_scope:
            raise ValueError(
                "verified bundle scope differs from the requested scenario"
            )
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"{result_key}=failed error={error}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    print(
        f"{result_key}={result['status']} scope={result['scope']} "
        f"host_gate={result['host_gate_status']} rf={result['rf']} result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
