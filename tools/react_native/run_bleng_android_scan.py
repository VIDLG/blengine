"""Run a managed formal Android scene observation with explicit fixture admission."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .android_lane_cli import add_fixture
from .bleng_android_scan import run_android_scan
from .bleng_scan_evidence import SCAN_SCENE, SCENES, SceneEvidence, verify_scan_bundle
from .rf_runner import add_journal_argument, reporter

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    # This is the managed UI-scene family, not a native instrumentation lane: it installs the
    # workbench App itself, so it names an App id and activity instead of an App APK pair and
    # states a wider fixture-start authorization. The fixture vocabulary is still the shared one.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--activity", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--apk-sha256", required=True)
    parser.add_argument(
        "--test-apk",
        type=Path,
        required=True,
        help="instrumentation APK that runs the scene inside the App's own process",
    )
    parser.add_argument("--test-apk-sha256", required=True)
    add_fixture(parser)
    parser.add_argument("--output", type=Path, required=True)
    add_journal_argument(parser)
    parser.add_argument(
        "--timeout",
        type=float,
        default=480,
        help="one finite budget for uninstall/install, the fixture and the UI scene",
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
        "--authorize-bluetooth-permissions",
        action="store_true",
        required=True,
        help="authorize exact BLUETOOTH_SCAN/CONNECT root grant with readback and exact restore; does not change radio",
    )
    parser.add_argument(
        "--use-root",
        action="store_true",
        help="explicit root-aware App installer, no fallback",
    )
    parser.add_argument(
        "--scene",
        choices=tuple(SCENES),
        default=SCAN_SCENE.name,
        help="managed workbench scene; selects the evidence scope, RF claim and expected trace",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    scene: SceneEvidence = SCENES[args.scene]
    try:
        with reporter(
            scene.name,
            prefix="formal_rn",
            output=args.output.absolute(),
            journal=args.progress_journal,
        ) as progress:
            result_path = run_android_scan(
                device=args.device,
                app_id=args.app_id,
                activity=args.activity,
                apk=args.apk.absolute(),
                apk_sha256=args.apk_sha256,
                test_apk=args.test_apk.absolute(),
                test_apk_sha256=args.test_apk_sha256,
                fixture_contract=args.fixture_contract.absolute(),
                fixture_profile=args.fixture_profile.absolute(),
                fixture_port=args.fixture_port,
                fixture_resource_identity=args.fixture_resource_identity,
                output_root=args.output.absolute(),
                timeout=args.timeout,
                cleanup_seconds=args.cleanup_seconds,
                authorize_install=args.authorize_install,
                authorize_fixture_start=args.authorize_fixture_start,
                authorize_bluetooth_permissions=args.authorize_bluetooth_permissions,
                use_root=args.use_root,
                cwd=PROJECT_ROOT,
                scene=scene,
                progress=progress,
            )
        result = verify_scan_bundle(result_path.parent)
        if result["scope"] != scene.scope("android"):
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
        print(f"formal_android_managed_scene=failed error={error}", file=sys.stderr)
        return 2
    passed = result["status"] == "passed"
    print(
        f"formal_android_managed_scene={result['status']} scene={scene.name} "
        f"scope={result['scope']} host_gate={result['host_gate_status']} "
        f"rf={result['rf']} result={result_path}",
        file=sys.stdout if passed else sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
