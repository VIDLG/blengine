"""Build the signed device `build-for-testing` products one native lane runs.

The lane itself never builds, signs or installs: it takes a signed `.app` and the manifest that
names it. This entry produces exactly those two things from the formal host project, using only
signing material already on this Mac (the same resolver the App device build uses), and prints the
tree digest the lane will refuse to run without.

It does not install, launch, register a device or touch an Apple account.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tools.apple import ios_preflight
from tools.react_native.bleng_ios_consumer import (
    HOST_SCHEME,
    IOS_ROOT,
    build,
    signed_bundle_id,
)
from tools.react_native.bleng_ios_multi_link import TEST_BUNDLE
from tools.react_native.bleng_ios_native_common import tree_sha256

DERIVED_DATA = IOS_ROOT / "build" / "device-derived"
# Debug, not Release: this target uses `@testable import BleHubReactNativeBleng`, which needs a
# module built with `-enable-testing`. A Release pod has testability off by construction, and the
# failure reads as "Unable to resolve Swift module dependency to a compatible module" — i.e. it
# looks like a missing slice, not a configuration choice. Testability is a property of the test
# harness here, not of the App under test.
CONFIGURATION = "Debug"
PRODUCTS = DERIVED_DATA / f"Build/Products/{CONFIGURATION}-iphoneos"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="physical iPhone UDID")
    parser.add_argument(
        "--refresh-pods",
        choices=("true", "false"),
        default="false",
        help=(
            "regenerate Pods before building; required after a BLeng spec/image change, "
            "because a stale preparation compiles against an older product"
        ),
    )
    parser.add_argument(
        "--accept-podfile-change",
        action="store_true",
        help=(
            "authorize one lockfile change; `pod install` otherwise runs with --deployment, "
            "which refuses a Podfile edit instead of silently re-resolving"
        ),
    )
    parser.add_argument(
        "--app", type=Path, help="report the identity of this App instead of building"
    )
    args = parser.parse_args(argv)
    if args.app is not None:
        return _report(args.app)
    try:
        # The same preparation the signed App build performs: staged image, Pods, then the
        # project. Skipping it is how a device build silently compiles against an older image.
        build(
            refresh_pods=args.refresh_pods == "true",
            allow_podfile_change=args.accept_podfile_change,
        )
        signing = ios_preflight.development_signing(args.device)
        bundle_id = signed_bundle_id(signing)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"formal_ios_device_test_build=failed error={error}", file=sys.stderr)
        return 2
    # A profile's own shape decides how it may be consumed; follow what the resolver
    # reported instead of assuming one kind. Nothing here requests a provisioning update.
    code_signing = (
        [
            "CODE_SIGN_STYLE=Automatic",
            "CODE_SIGN_IDENTITY=Apple Development",
            f"DEVELOPMENT_TEAM={signing['team']}",
        ]
        if signing["profile_is_xcode_managed"]
        else [
            "CODE_SIGN_STYLE=Manual",
            f"CODE_SIGN_IDENTITY={signing['identity']}",
            f"DEVELOPMENT_TEAM={signing['team']}",
            f"PROVISIONING_PROFILE_SPECIFIER={signing['profile']}",
        ]
    )
    command = [
        "xcodebuild",
        "-workspace",
        str(IOS_ROOT / f"{HOST_SCHEME}.xcworkspace"),
        "-scheme",
        HOST_SCHEME,
        "-configuration",
        CONFIGURATION,
        "-sdk",
        "iphoneos",
        "-destination",
        f"platform=iOS,id={signing['device']}",
        "-derivedDataPath",
        str(DERIVED_DATA),
        *code_signing,
        f"PRODUCT_BUNDLE_IDENTIFIER={bundle_id}",
        "ONLY_ACTIVE_ARCH=YES",
        # Explicit, so a project-level default cannot silently disable `@testable import`.
        "ENABLE_TESTABILITY=YES",
        "build-for-testing",
    ]
    completed = subprocess.run(
        command, cwd=IOS_ROOT, text=True, encoding="utf-8", check=False
    )
    if completed.returncode:
        print(
            f"formal_ios_device_test_build=failed exit={completed.returncode}",
            file=sys.stderr,
        )
        return 2
    app = PRODUCTS / f"{HOST_SCHEME}.app"
    xctestrun = next(DERIVED_DATA.glob("Build/Products/*.xctestrun"), None)
    if not app.is_dir():
        print(
            f"formal_ios_device_test_build=failed error=missing app: {app}",
            file=sys.stderr,
        )
        return 2
    # A hosted test bundle is embedded in the App's PlugIns, which is also where the installed
    # product looks for it; a logic-test bundle sits beside the App instead.
    bundle = next(
        (
            candidate
            for candidate in (
                app / "PlugIns" / TEST_BUNDLE,
                PRODUCTS / TEST_BUNDLE,
            )
            if candidate.exists()
        ),
        None,
    )
    if bundle is None:
        print(
            "formal_ios_device_test_build=failed error=no "
            f"{TEST_BUNDLE} next to or inside the App",
            file=sys.stderr,
        )
        return 2
    if xctestrun is None:
        print(
            "formal_ios_device_test_build=failed error=no xctestrun in derived data",
            file=sys.stderr,
        )
        return 2
    return _report(app, xctestrun=xctestrun)


def _report(app: Path, *, xctestrun: Path | None = None) -> int:
    if not app.is_dir() or app.is_symlink():
        print(
            f"formal_ios_device_test_build=failed error=missing App: {app}",
            file=sys.stderr,
        )
        return 2
    if not (app / "embedded.mobileprovision").is_file():
        print(
            "formal_ios_device_test_build=failed error=App is not signed",
            file=sys.stderr,
        )
        return 2
    print(f"formal_ios_device_app={app}")
    print(f"formal_ios_device_app_tree_sha256={tree_sha256(app)}")
    if xctestrun is not None:
        print(f"formal_ios_device_xctestrun={xctestrun}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
