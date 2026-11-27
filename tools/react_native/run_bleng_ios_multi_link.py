"""Run one managed iOS multi-link isolation scene with explicit fixture admission.

Every peer is declared on the command line as its own leased fixture, so this runner carries no
device name, port or profile: the declaration *is* the topology. The signed App and the
`build-for-testing` manifest are inputs; this entry never builds, signs or installs anything on
its own authority.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from .bleng_ios_multi_link import SCOPE, FixtureSpec, run_multi_link
from .bleng_multi_link_contract import (
    MAXIMUM_PEERS,
    MINIMUM_PEERS,
    Fault,
    parse_fault,
    parse_peer,
    require_rounds,
)
from .rf_runner import add_journal_argument, reporter

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="physical iPhone UDID")
    parser.add_argument(
        "--app", type=Path, required=True, help="signed device .app the manifest names"
    )
    parser.add_argument(
        "--app-tree-sha256",
        required=True,
        help="tree digest of that signed .app; the run refuses a different tree",
    )
    parser.add_argument(
        "--xctestrun",
        type=Path,
        required=True,
        help="signed build-for-testing manifest; its test host must be --app",
    )
    parser.add_argument(
        "--peer",
        type=_peer,
        action="append",
        required=True,
        metavar="port=PORT,contract=PATH,profile=PATH,identity=jlink:SERIAL",
        help=(
            f"one declared peer, repeatable ({MINIMUM_PEERS}..{MAXIMUM_PEERS}); the first "
            "declared peer is the link this scene ends, the rest must survive"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    add_journal_argument(parser)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--cleanup-seconds", type=float, default=180)
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
        "--authorize-install",
        action="store_true",
        required=True,
        help="authorize installing this signed App and test bundle; no other bundle is touched",
    )
    parser.add_argument(
        "--authorize-fixture-start",
        action="store_true",
        required=True,
        help="authorize exact-profile GATT initialization and advertising only; never flash/reset",
    )
    parser.add_argument(
        "--diagnose-notifications",
        action="store_true",
        help=(
            "keep the Apple backend's notification diagnostics in the xcodebuild log; "
            "evidence only, never a scene input"
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
    args = parser.parse_args(argv)
    if not MINIMUM_PEERS <= len(args.peer) <= MAXIMUM_PEERS:
        parser.error(
            f"--peer must be declared {MINIMUM_PEERS}..{MAXIMUM_PEERS} times, one per fixture"
        )
    if len({peer["port"] for peer in args.peer}) != len(args.peer):
        parser.error("each --peer needs its own port")
    if len({peer["identity"] for peer in args.peer}) != len(args.peer):
        parser.error("each --peer needs its own canonical fixture identity")
    if re.fullmatch(r"[0-9a-f]{64}", args.app_tree_sha256) is None:
        parser.error("--app-tree-sha256 must be a lowercase SHA-256")
    try:
        with reporter(
            "multi-link",
            prefix="formal_ios",
            output=args.output.absolute(),
            journal=args.progress_journal,
        ):
            result_path = run_multi_link(
                device=args.device,
                app=args.app.absolute(),
                app_tree_sha256=args.app_tree_sha256,
                xctestrun=args.xctestrun.absolute(),
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
                diagnose_notifications=args.diagnose_notifications,
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
        print(f"formal_ios_native_multi_link=failed error={error}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_ios_native_multi_link={result['status']} "
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
