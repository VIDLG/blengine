"""Run the formal App's permission-authorization UI test on one physical iPhone.

Consumes the signed App + manifest already produced by the device test build; never builds,
signs, installs by hand or touches RF. The one destructive action is the App's own Bluetooth
authorization reset/restore, which is why it has its own explicit authorization flag.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .bleng_ios_permission_ui import SCOPE, run_permission_ui
from .rf_runner import add_journal_argument, reporter

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="physical iPhone UDID")
    parser.add_argument(
        "--xctestrun", type=Path, required=True, help="build-for-testing manifest"
    )
    parser.add_argument("--output", type=Path, required=True)
    add_journal_argument(parser)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument(
        "--restore-only",
        action="store_true",
        help=(
            "only prove the App is currently authorized; never reset the authorization "
            "(used to bring a device into the gate's precondition)"
        ),
    )
    parser.add_argument(
        "--authorize-permission-reset",
        action="store_true",
        required=True,
        help=(
            "authorize the lane to clear and restore this App's Bluetooth authorization "
            "through XCUITest; no other app, radio state or fixture is touched"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with reporter(
            "ios-permission-ui",
            prefix="formal_rn",
            output=args.output.absolute(),
            journal=args.progress_journal,
        ):
            result_path = run_permission_ui(
                device=args.device,
                xctestrun=args.xctestrun.absolute(),
                output_root=args.output.absolute(),
                timeout=args.timeout,
                restore_only=args.restore_only,
                authorize_permission_reset=args.authorize_permission_reset,
                cwd=PROJECT_ROOT,
            )
        result = json.loads(result_path.read_bytes())
        if result["scope"] != SCOPE:
            raise ValueError("published result is not this lane's scope")
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"formal_ios_permission_ui=failed error={error}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    phases = result.get("phases") or {}
    rendered = " ".join(
        f"{phase}={value['test_summary'].get('passedTests')}/"
        f"{value['test_summary'].get('failedTests')}/"
        f"{value['test_summary'].get('skippedTests')}"
        for phase, value in phases.items()
        if isinstance(value.get("test_summary"), dict)
    )
    print(
        f"formal_ios_permission_ui={result['status']} scope={result['scope']} "
        f"restore_only={str(result['restore_only']).lower()} "
        f"phases(pass/fail/skip)={rendered} result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
