"""Run a managed formal iOS scene observation with explicit fixture admission."""

from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

from tools.apple.ios_preflight import development_signing

from .bleng_ios_scan import MAX_TOTAL_SECONDS, run_ios_scan
from .bleng_scan_evidence import SCAN_SCENE, SCENES, SceneEvidence, verify_scan_bundle
from .rf_runner import add_journal_argument, reporter

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _app_identity(app: Path) -> tuple[str, str]:
    """The bundle id and executable the frozen App itself declares."""
    info = plistlib.loads((app / "Info.plist").read_bytes())
    if type(info) is not dict:
        raise ValueError("App Info.plist is not a dictionary")
    bundle_id, executable = (
        info.get("CFBundleIdentifier"),
        info.get("CFBundleExecutable"),
    )
    if type(bundle_id) is not str or not bundle_id:
        raise ValueError("App Info.plist does not declare a bundle identifier")
    if type(executable) is not str or not executable:
        raise ValueError("App Info.plist does not declare an executable")
    return bundle_id, executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument(
        "--app",
        type=Path,
        default=(
            PROJECT_ROOT
            / "tests/consumers/react-native-bleng/ios/build/device-derived"
            / "Build/Products/Debug-iphoneos/BleHubBlengHost.app"
        ),
        help="signed device .app; the default is the formal consumer's device build",
    )
    parser.add_argument(
        "--app-tree-sha256",
        required=True,
        help="reviewed App tree fingerprint; the producer refuses a different tree",
    )
    parser.add_argument("--app-id", help="defaults to the App's own CFBundleIdentifier")
    parser.add_argument("--executable", help="defaults to the App's CFBundleExecutable")
    parser.add_argument(
        "--team", help="defaults to the local Team for Apple Development"
    )
    parser.add_argument(
        "--identity", help="defaults to the local Apple Development identity name"
    )
    parser.add_argument(
        "--xctestrun",
        type=Path,
        required=True,
        help="the signed host build's .xctestrun: the scene runs inside that target",
    )
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--fixture-port", required=True)
    parser.add_argument(
        "--fixture-resource-identity",
        required=True,
        help="explicit canonical jlink:<serial> or nrf-usb:<serial>; no port-only identity",
    )
    parser.add_argument("--output", type=Path, required=True)
    add_journal_argument(parser)
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help=(
            "one finite budget for the frozen App, install/launch, the test target, "
            f"fixture and the UI scene (at most {MAX_TOTAL_SECONDS}s)"
        ),
    )
    parser.add_argument("--cleanup-seconds", type=float, default=90)
    parser.add_argument("--authorize-install", action="store_true", required=True)
    parser.add_argument(
        "--authorize-fixture-start",
        action="store_true",
        required=True,
        help="authorize exact-profile GATT initialization and advertising only after leased live installed-image proof; never firmware flash/reset",
    )
    parser.add_argument(
        "--release-before-install",
        action="store_true",
        help=(
            "terminate this bundle's own already-running copy (exact PID from the "
            "candidate inventory, recorded) before installing the frozen tree; without "
            "it a running copy still refuses takeover"
        ),
    )
    parser.add_argument(
        "--scene",
        choices=tuple(SCENES),
        default=SCAN_SCENE.name,
        help="managed workbench scene; selects the evidence scope, RF claim and expected trace",
    )
    parser.add_argument(
        "--release-retained-app",
        action="store_true",
        help=(
            "stop the App this run launched even when the scene passes; a passing run "
            "otherwise keeps it running as the evidence a human inspects. Set this when "
            "running a sequence of scenes, so the next one starts from a device with "
            "nothing of ours running instead of refusing takeover"
        ),
    )
    args = parser.parse_args(argv)
    scene: SceneEvidence = SCENES[args.scene]
    app = args.app.absolute()
    try:
        app_id, executable = _app_identity(app)
        signing = development_signing(args.device)
        with reporter(
            scene.name,
            prefix="formal_rn",
            output=args.output.absolute(),
            journal=args.progress_journal,
        ) as progress:
            result_path = run_ios_scan(
                device=args.device,
                app=app,
                app_tree_sha256=args.app_tree_sha256,
                app_id=args.app_id or app_id,
                executable=args.executable or executable,
                team=args.team or signing["team"],
                identity=args.identity or signing["identity"],
                xctestrun=args.xctestrun.absolute(),
                fixture_contract=args.fixture_contract.absolute(),
                fixture_profile=args.fixture_profile.absolute(),
                fixture_port=args.fixture_port,
                fixture_resource_identity=args.fixture_resource_identity,
                output_root=args.output.absolute(),
                timeout=args.timeout,
                cleanup_seconds=args.cleanup_seconds,
                authorize_install=args.authorize_install,
                authorize_fixture_start=args.authorize_fixture_start,
                release_retained_app=args.release_retained_app,
                cwd=PROJECT_ROOT,
                scene=scene,
                progress=progress,
                release_before_install=args.release_before_install,
            )
        result = verify_scan_bundle(result_path.parent)
        if result["scope"] != scene.scope("ios"):
            raise ValueError(
                f"verified bundle scope differs from the {scene.name} scene"
            )
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"formal_ios_managed_scene=failed error={error}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_ios_managed_scene={result['status']} scene={scene.name} "
        f"scope={result['scope']} host_gate={result['host_gate_status']} "
        f"rf={result['rf']} result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
