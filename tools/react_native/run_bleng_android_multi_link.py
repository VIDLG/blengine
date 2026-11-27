"""Run one managed Android multi-link isolation scene with explicit fixture admission.

Every peer is declared on the command line as its own leased fixture, so this runner
carries no device name, port or profile: the declaration *is* the topology.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from .android_lane_cli import add_authorization, add_budget, add_candidate, add_device
from .bleng_android_multi_link import (
    MAXIMUM_PEERS,
    MINIMUM_PEERS,
    SCOPE,
    FixtureSpec,
    run_multi_link,
)
from .bleng_multi_link_contract import Fault, parse_fault, parse_peer, require_rounds
from .rf_runner import reporter

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _peer(text: str) -> dict[str, str]:
    try:
        return parse_peer(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _rounds(text: str) -> int:
    try:
        return require_rounds(int(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _fault(text: str) -> Fault | None:
    try:
        return parse_fault(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_device(parser)
    add_candidate(parser)
    parser.add_argument(
        "--peer",
        type=_peer,
        action="append",
        required=True,
        metavar="port=PORT,contract=PATH,profile=PATH,identity=jlink:SERIAL",
        help=(
            f"one declared peer, repeatable ({MINIMUM_PEERS}..{MAXIMUM_PEERS}); first declared "
            "peer is the link this scene ends, the rest must survive"
        ),
    )
    add_budget(parser, timeout=900, cleanup_seconds=120)
    parser.add_argument(
        "--rounds",
        type=_rounds,
        default=1,
        help=(
            "repeat the whole scene this many times (1..10); a soak is this parameter, not "
            "another runner"
        ),
    )
    parser.add_argument(
        "--fault",
        type=_fault,
        default=None,
        metavar="write-reject:INDEX",
        help=(
            "refuse that peer's write inside its own firmware; the scene then requires a reported "
            "refusal and an unchanged read-back on that link only"
        ),
    )
    add_authorization(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not MINIMUM_PEERS <= len(args.peer) <= MAXIMUM_PEERS:
        parser.error(
            f"--peer must be declared {MINIMUM_PEERS}..{MAXIMUM_PEERS} times, one per fixture"
        )
    if len({peer["port"] for peer in args.peer}) != len(args.peer):
        parser.error("each --peer needs its own port")
    if len({peer["identity"] for peer in args.peer}) != len(args.peer):
        parser.error("each --peer needs its own canonical fixture identity")
    for sha in (args.app_apk_sha256, args.test_apk_sha256):
        if re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            parser.error("APK digests must be lowercase SHA-256")
    try:
        with reporter(
            "multi-link",
            prefix="formal_rn",
            output=args.output.absolute(),
            journal=args.progress_journal,
        ):
            result_path = run_multi_link(
                device=args.device,
                app_apk=args.app_apk.absolute(),
                app_apk_sha256=args.app_apk_sha256,
                test_apk=args.test_apk.absolute(),
                test_apk_sha256=args.test_apk_sha256,
                fixtures=[
                    FixtureSpec(
                        port=peer["port"],
                        contract=Path(peer["contract"]).absolute(),
                        profile=Path(peer["profile"]).absolute(),
                        resource_identity=peer["identity"],
                    )
                    for peer in args.peer
                ],
                output_root=args.output.absolute(),
                timeout=args.timeout,
                cleanup_seconds=args.cleanup_seconds,
                authorize_install=args.authorize_install,
                authorize_fixture_start=args.authorize_fixture_start,
                authorize_bluetooth_permissions=args.authorize_bluetooth_permissions,
                use_root=args.use_root,
                rounds=args.rounds,
                fault=args.fault,
                cwd=PROJECT_ROOT,
            )
        result = json.loads(result_path.read_bytes())
        if result["scope"] != SCOPE:
            raise ValueError("published scene scope differs from the multi-link scope")
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"formal_android_native_multi_link=failed error={error}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_android_native_multi_link={result['status']} "
        f"scope={result['scope']} host_gate={result['host_gate_status']} "
        f"rf={result['rf']} peers={len(result['peers'])} "
        f"rounds={len(result['rounds'])} "
        f"stages={','.join(result['rounds'][-1]['stages']) if result['rounds'] else ''} "
        f"result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
