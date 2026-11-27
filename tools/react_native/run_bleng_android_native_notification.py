"""Run one managed native notification scene with explicit fixture admission."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .android_lane_cli import (
    add_authorization,
    add_budget,
    add_candidate,
    add_device,
    add_fixture,
)
from .bleng_android_native_notification import SCOPE, run_native_notification

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device(parser)
    add_candidate(parser)
    add_fixture(parser)
    parser.add_argument(
        "--characteristic-role",
        default="updates",
        help="fixture profile role this scene subscribes to (default: updates)",
    )
    parser.add_argument(
        "--scene",
        choices=("single", "sequence", "overflow"),
        default="single",
        help="single value, repeated values (no loss/duplication), or slow-consumer overflow",
    )
    parser.add_argument("--value-count", type=int, default=8)
    parser.add_argument(
        "--drain-delay-seconds",
        type=float,
        default=8.0,
        help="how long the overflow scene idles before its one read (host needs this window)",
    )
    add_budget(parser, timeout=480, cleanup_seconds=90)
    add_authorization(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        path = run_native_notification(
            device=args.device,
            app_apk=args.app_apk.absolute(),
            app_apk_sha256=args.app_apk_sha256,
            test_apk=args.test_apk.absolute(),
            test_apk_sha256=args.test_apk_sha256,
            fixture_contract=args.fixture_contract.absolute(),
            fixture_profile=args.fixture_profile.absolute(),
            fixture_port=args.fixture_port,
            fixture_resource_identity=args.fixture_resource_identity,
            characteristic_role=args.characteristic_role,
            scene=args.scene,
            value_count=args.value_count,
            drain_delay_seconds=args.drain_delay_seconds,
            output_root=args.output.absolute(),
            timeout=args.timeout,
            cleanup_seconds=args.cleanup_seconds,
            authorize_install=args.authorize_install,
            authorize_fixture_start=args.authorize_fixture_start,
            authorize_bluetooth_permissions=args.authorize_bluetooth_permissions,
            use_root=args.use_root,
            cwd=PROJECT_ROOT,
        )
        result = json.loads(path.read_bytes())
        if result["scope"] != SCOPE:
            raise ValueError(
                "published scene scope differs from the native notification scope"
            )
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(
            f"formal_android_native_notification=failed error={error}", file=sys.stderr
        )
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_android_native_notification={result['status']} "
        f"scope={result['scope']} host_gate={result['host_gate_status']} "
        f"rf={result['rf']} scene={result['scene']} "
        f"stages={','.join(result['stages'])} result={path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
