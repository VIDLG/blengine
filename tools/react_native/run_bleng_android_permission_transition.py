"""Run the three-phase managed Android permission transition scene.

The host owns every OS action (revoke, grant, permission-death proof); the formal App owns
the product facts in three separate processes. No UI automation server and no retired
consumer App in this path.
"""

from __future__ import annotations

import argparse
import json
import re
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
from .bleng_android_permission_transition import (
    SCOPE,
    run_permission_transition,
)
from .rf_runner import reporter

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# This lane is the only one that revokes a grant and proves the OS kill, so its authorization
# describes that wider action; the flag itself stays the shared one.
BLUETOOTH_PERMISSIONS_HELP = (
    "authorize revoking/granting BLUETOOTH_SCAN/CONNECT with readback, the OS death "
    "proof and exact restore"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device(parser)
    add_candidate(parser)
    add_fixture(parser)
    add_budget(parser, timeout=900, cleanup_seconds=180)
    add_authorization(parser, bluetooth_permissions_help=BLUETOOTH_PERMISSIONS_HELP)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for sha in (args.app_apk_sha256, args.test_apk_sha256):
        if re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            parser.error("APK digests must be lowercase SHA-256")
    if not args.fixture_port.strip():
        parser.error("--fixture-port must not be blank")
    try:
        with reporter(
            "permission-transition",
            prefix="formal_rn",
            output=args.output.absolute(),
            journal=args.progress_journal,
        ):
            result_path = run_permission_transition(
                device=args.device,
                app_apk=args.app_apk.absolute(),
                app_apk_sha256=args.app_apk_sha256,
                test_apk=args.test_apk.absolute(),
                test_apk_sha256=args.test_apk_sha256,
                fixture_port=args.fixture_port,
                fixture_contract=args.fixture_contract.absolute(),
                fixture_profile=args.fixture_profile.absolute(),
                fixture_resource_identity=args.fixture_resource_identity,
                output_root=args.output.absolute(),
                timeout=args.timeout,
                cleanup_seconds=args.cleanup_seconds,
                authorize_install=args.authorize_install,
                authorize_fixture_start=args.authorize_fixture_start,
                authorize_bluetooth_permissions=args.authorize_bluetooth_permissions,
                use_root=args.use_root,
                cwd=PROJECT_ROOT,
            )
        result = json.loads(result_path.read_bytes())
        if result["scope"] != SCOPE:
            raise ValueError(
                "published scene scope differs from the permission transition scope"
            )
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(
            f"formal_android_native_permission_transition=failed error={error}",
            file=sys.stderr,
        )
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_android_native_permission_transition={result['status']} "
        f"scope={result['scope']} host_gate={result['host_gate_status']} "
        f"rf={result['rf']} hold={result.get('hold', '')} "
        f"death_reason={result.get('permission_death', {}).get('reason', '')} "
        f"phases={','.join(result['phases'])} "
        f"result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
