"""Compile the isolated Swift contract and formal source/local-package RN iOS host.

This is build-only, not a clean npm tarball or artifact-only consumer gate.
Dependencies, generated Nitro glue and the combined Apple package must already
be prepared by the explicit host-check, bleng-check and staging recipes.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from tools.apple import xcodegen_toolchain

ROOT = Path(__file__).resolve().parents[2]
HOST_ROOT = ROOT / "tests/consumers/react-native-bleng"
IOS_ROOT = HOST_ROOT / "ios"
FORMAL_PACKAGE_ROOT = HOST_ROOT / "node_modules/@blehub/react-native-bleng"
FORMAL_PODSPEC = FORMAL_PACKAGE_ROOT / "BleHubReactNativeBleng.podspec"
RN_STAGED_ROOT = (
    Path(
        os.environ.get(
            "BLEHUB_APPLE_BLENG_STAGING_ROOT",
            str(ROOT / ".work/staged/apple-bleng-react-native"),
        )
    )
    .expanduser()
    .absolute()
)
SPEC = IOS_ROOT / "PlatformContract/project.yml"
SMOKE_ROOT = SPEC.parent / "Sources"
WORK_ROOT = ROOT / ".work/react-native-bleng-ios-consumer/PlatformContract"
RENDERED_SPEC = WORK_ROOT / "project.yml"
SCHEME = "BlengPlatformContract"
PROJECT = WORK_ROOT / f"{SCHEME}.xcodeproj"
DERIVED_DATA = WORK_ROOT / "DerivedData"
PRODUCT = DERIVED_DATA / f"Build/Products/Debug-iphonesimulator/lib{SCHEME}.a"
SWIFT_MODULE = (
    DERIVED_DATA / f"Build/Products/Debug-iphonesimulator/{SCHEME}.swiftmodule"
)
HOST_SCHEME = "BleHubBlengHost"
HOST_PROJECT = IOS_ROOT / f"{HOST_SCHEME}.xcodeproj/project.pbxproj"
WORKSPACE = IOS_ROOT / f"{HOST_SCHEME}.xcworkspace"
HOST_DERIVED_DATA = IOS_ROOT / "build/simulator-derived"
NATIVE_TEST_SOURCE = (
    IOS_ROOT / "BleHubBlengHostNativeTests/BleHubBlengHostNativeTests.swift"
)
LOG_ROOT = ROOT / ".work/logs"
PODSPEC_LOG = LOG_ROOT / "react-native-bleng-ios-podspec.log"
XCODEGEN_LOG = LOG_ROOT / "react-native-bleng-ios-platform-xcodegen.log"
CONTRACT_LOG = LOG_ROOT / "react-native-bleng-ios-platform-build.log"
POD_LOG = LOG_ROOT / "react-native-bleng-ios-pod-install.log"
HOST_LOG = LOG_ROOT / "react-native-bleng-ios-native-test-build.log"
STAGING_TOKEN = "__BLEHUB_APPLE_BLENG_STAGING_ROOT__"
PACKAGE_TOKEN = "__BLEHUB_INSTALLED_FORMAL_PACKAGE_ROOT__"
SMOKE_TOKEN = "__BLEHUB_PLATFORM_CONTRACT_SMOKE_ROOT__"
PLATFORM_SOURCES = (
    "ApplePlatformSession.swift",
    "RecoveryEventProjection.swift",
    "RecoveryEventInbox.swift",
    "AppleRustSupervisorOwner.swift",
)
STAGED_FILES = (
    "BleHubApple/Package.swift",
    "BleHubApple/BleHubApple.podspec",
    "BleHubUniFfi/Package.swift",
    "BleHubUniFfi/BleHubUniFfi.podspec",
    "BleHubUniFfi/Sources/BleHubUniFfi/blehub.swift",
    "BleHubUniFfi/Sources/BleHubUniFfi/bleng.swift",
    "BleHubUniFfi/Sources/BleHubUniFfi/bleng_supervisor.swift",
    "BleHubUniFfi/BleHubUniFfiNative.xcframework/Info.plist",
    *(
        f"BleHubUniFfi/BleHubUniFfiNative.xcframework/{variant}/{name}"
        for variant in ("ios-arm64", "ios-arm64_x86_64-simulator")
        for name in (
            "libbleng_uniffi.a",
            "Headers/blehubFFI.h",
            "Headers/blengFFI.h",
            "Headers/bleng_supervisorFFI.h",
            "Headers/module.modulemap",
        )
    ),
)


class BlengIOSConsumerError(RuntimeError):
    """The build-only formal consumer boundary could not be validated."""


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise BlengIOSConsumerError(f"missing required executable: {name}")
    return executable


def _run(
    command: list[str],
    *,
    cwd: Path,
    log: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    failure = ""
    with log.open("w", encoding="utf-8") as output:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            failure = f"command timed out after {timeout}s"
        else:
            if result.returncode:
                failure = f"command failed with exit code {result.returncode}"
    if failure:
        tail = "\n".join(
            log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        )
        raise BlengIOSConsumerError(f"{failure}; full log: {log}\n{tail}")


def validate_formal_podspec() -> None:
    if not FORMAL_PODSPEC.is_file():
        raise BlengIOSConsumerError(
            f"missing installed formal BLeng podspec: {FORMAL_PODSPEC}; "
            "run `pixi run just react-native formal-host-check` first"
        )
    _run(
        [_require_executable("pod"), "ipc", "spec", str(FORMAL_PODSPEC)],
        cwd=HOST_ROOT,
        log=PODSPEC_LOG,
        timeout=120,
    )
    output = PODSPEC_LOG.read_text(encoding="utf-8")
    # CocoaPods can emit warnings before its JSON object.
    start = output.find("{")
    try:
        spec = json.loads(output[start:] if start >= 0 else output)
    except ValueError as error:
        raise BlengIOSConsumerError(
            f"CocoaPods returned no valid JSON for the formal BLeng podspec; log: {PODSPEC_LOG}"
        ) from error
    if not isinstance(spec, dict):
        raise BlengIOSConsumerError("formal BLeng podspec JSON is not an object")
    dependencies = spec.get("dependencies")
    if not isinstance(dependencies, dict) or not {
        "BleHubApple",
        "BleHubUniFfi",
        "NitroModules",
    }.issubset(dependencies):
        raise BlengIOSConsumerError(
            "formal BLeng podspec is missing one of its production dependencies"
        )
    source_files = spec.get("source_files")
    if not isinstance(source_files, list) or not any(
        Path(str(path)).name == "HybridBlengSessionOwner.swift" for path in source_files
    ):
        raise BlengIOSConsumerError(
            "formal BLeng podspec does not expose HybridBlengSessionOwner.swift"
        )


def build() -> None:
    if platform.system() != "Darwin":
        raise BlengIOSConsumerError("the formal iOS consumer requires macOS/Xcode")
    missing = [
        str(RN_STAGED_ROOT / name)
        for name in STAGED_FILES
        if not (RN_STAGED_ROOT / name).is_file()
    ]
    if missing:
        raise BlengIOSConsumerError(
            "formal combined Apple staging is incomplete; run "
            "`pixi run just react-native stage-apple-bleng` first: "
            + ", ".join(missing)
        )
    installed = (
        FORMAL_PODSPEC,
        *(FORMAL_PACKAGE_ROOT / "ios" / name for name in PLATFORM_SOURCES),
    )
    missing = [str(path) for path in installed if not path.is_file()]
    if missing:
        raise BlengIOSConsumerError(
            "formal installed package is incomplete; run "
            "`pixi run just react-native formal-host-check` first: "
            + ", ".join(missing)
        )
    required = (
        HOST_PROJECT,
        IOS_ROOT / "Podfile",
        NATIVE_TEST_SOURCE,
        SPEC,
        SMOKE_ROOT / "LifecycleCompileSmoke.swift",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BlengIOSConsumerError(
            "formal host/native contract inputs are incomplete: " + ", ".join(missing)
        )
    validate_formal_podspec()
    template = SPEC.read_text(encoding="utf-8")
    for token, path in (
        (STAGING_TOKEN, RN_STAGED_ROOT),
        (PACKAGE_TOKEN, FORMAL_PACKAGE_ROOT),
        (SMOKE_TOKEN, SMOKE_ROOT),
    ):
        if token not in template:
            raise BlengIOSConsumerError(
                f"PlatformContract spec is missing token: {token}"
            )
        template = template.replace(token, json.dumps(str(path))[1:-1])
    xcodegen = xcodegen_toolchain.verify()
    xcodebuild = _require_executable("xcodebuild")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    RENDERED_SPEC.write_text(template, encoding="utf-8", newline="\n")
    _run(
        [str(xcodegen), "generate", "--spec", str(RENDERED_SPEC)],
        cwd=WORK_ROOT,
        log=XCODEGEN_LOG,
        timeout=120,
    )
    if not (PROJECT / "project.pbxproj").is_file():
        raise BlengIOSConsumerError(
            f"XcodeGen did not produce the PlatformContract project: {PROJECT}"
        )
    unsigned = [
        "-configuration",
        "Debug",
        "-sdk",
        "iphonesimulator",
        "-destination",
        "generic/platform=iOS Simulator",
        "CODE_SIGNING_ALLOWED=NO",
        "CODE_SIGNING_REQUIRED=NO",
    ]
    _run(
        [
            xcodebuild,
            "-project",
            str(PROJECT),
            "-scheme",
            SCHEME,
            "-derivedDataPath",
            str(DERIVED_DATA),
            *unsigned,
            "build",
        ],
        cwd=WORK_ROOT,
        log=CONTRACT_LOG,
        timeout=180,
    )
    if not PRODUCT.is_file() or PRODUCT.stat().st_size == 0:
        raise BlengIOSConsumerError(
            f"missing PlatformContract static product: {PRODUCT}"
        )
    if not SWIFT_MODULE.is_dir():
        raise BlengIOSConsumerError(
            f"missing PlatformContract Swift module: {SWIFT_MODULE}"
        )
    # Match the Podfile's override without leaking it into the controller process.
    env = dict(os.environ, BLEHUB_APPLE_BLENG_STAGING_ROOT=str(RN_STAGED_ROOT))
    _run(
        [_require_executable("pod"), "install", "--no-repo-update"],
        cwd=IOS_ROOT,
        log=POD_LOG,
        timeout=180,
        env=env,
    )
    if not WORKSPACE.is_dir():
        raise BlengIOSConsumerError(
            f"CocoaPods did not produce the formal host workspace: {WORKSPACE}"
        )
    _run(
        [
            xcodebuild,
            "-workspace",
            str(WORKSPACE),
            "-scheme",
            HOST_SCHEME,
            "-derivedDataPath",
            str(HOST_DERIVED_DATA),
            *unsigned,
            "-only-testing:BleHubBlengHostNativeTests",
            "build-for-testing",
        ],
        cwd=IOS_ROOT,
        log=HOST_LOG,
        timeout=180,
        env=env,
    )
    products = HOST_DERIVED_DATA / "Build/Products/Debug-iphonesimulator"
    for executable in (
        products / f"{HOST_SCHEME}.app" / HOST_SCHEME,
        products / "BleHubBlengHostNativeTests.xctest/BleHubBlengHostNativeTests",
    ):
        if not executable.is_file() or executable.stat().st_size == 0:
            raise BlengIOSConsumerError(
                f"missing formal host/test executable: {executable}"
            )
    print(f"bleng_platform_contract_product={PRODUCT}", flush=True)
    print(f"bleng_formal_host_native_test_derived_data={HOST_DERIVED_DATA}", flush=True)


def main() -> int:
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
