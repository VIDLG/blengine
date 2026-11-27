"""Compile the isolated Swift contract and formal source/local-package RN iOS host.

This is build-only, not a clean npm tarball or artifact-only consumer gate.
Dependencies, generated Nitro glue and the combined Apple package must already
be prepared by the explicit host-check, bleng-check and staging recipes.
"""

from __future__ import annotations

import argparse
import builtins
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from traceback import format_exception_only

from tools.apple import consumer_bleng_ios as consumer
from tools.apple import ios_preflight, xcodegen_toolchain
from tools.react_native import ios_pod_preparation, ios_pod_refresh

ROOT = Path(__file__).resolve().parents[2]
HOST_ROOT = ROOT / "tests/consumers/react-native-bleng"
IOS_ROOT = HOST_ROOT / "ios"
FORMAL_PACKAGE_ROOT = HOST_ROOT / "node_modules/@blengine/react-native-bleng"
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
NATIVE_TEST_ENTRY = IOS_ROOT / "BleHubBlengHostNativeTests/BlengHostReload.js"
NATIVE_TEST_BUNDLE = IOS_ROOT / "build/native-test-resources/BlengHostReload.jsbundle"
RN_CLI = HOST_ROOT / "node_modules/react-native/cli.js"
LOG_ROOT = ROOT / ".work/logs"
PODSPEC_LOG = LOG_ROOT / "react-native-bleng-ios-podspec.log"
XCODEGEN_LOG = LOG_ROOT / "react-native-bleng-ios-platform-xcodegen.log"
CONTRACT_LOG = LOG_ROOT / "react-native-bleng-ios-platform-build.log"
POD_LOG = LOG_ROOT / "react-native-bleng-ios-pod-install.log"
POD_RECEIPT = ROOT / ".work/react-native-bleng-ios-consumer/pods-preparation.json"
BUILD_LOCK = POD_RECEIPT.with_suffix(".lock")
HOST_LOG = LOG_ROOT / "react-native-bleng-ios-native-test-build.log"
BUNDLE_LOG = LOG_ROOT / "react-native-bleng-ios-native-test-bundle.log"
# A device build is a different product from the simulator one: different SDK, signed,
# and one architecture. It gets its own derived data so neither build can reuse the
# other's products or sign state.
DEVICE_DERIVED_DATA = IOS_ROOT / "build/device-derived"
DEVICE_LOG = LOG_ROOT / "react-native-bleng-ios-device-build.log"
# One finite budget for a from-scratch device Release build of the RN graph.
DEVICE_BUILD_SECONDS = 2700.0
# The bundle id this App signs as when the local profile is a Team wildcard. The suffix
# is the App's identity, not a placeholder: an installed App is recognised by it.
DEVICE_BUNDLE_SUFFIX = "bleng-rn-host"
STAGING_TOKEN = "__BLEHUB_APPLE_BLENG_STAGING_ROOT__"
# The canonical Apple backend source and the path it is staged under. The signed product links
# the staged copy, so these two must agree or a rebuild silently ships the older backend.
APPLE_BACKEND_SOURCES = ROOT / "platforms/apple/Sources/BleHubApple"
APPLE_BACKEND_STAGED = "BleHubApple/Sources/BleHubApple"
PACKAGE_TOKEN = "__BLEHUB_INSTALLED_FORMAL_PACKAGE_ROOT__"
SMOKE_TOKEN = "__BLEHUB_PLATFORM_CONTRACT_SMOKE_ROOT__"
PLATFORM_SOURCES = (
    "ApplePlatformSession.swift",
    "RecoveryEventProjection.swift",
    "RecoveryEventInbox.swift",
    "AppleRustSupervisorOwner.swift",
)
LIFECYCLE_SOURCES = (
    "BlengEngineLifecycle.h",
    "BlengEngineLifecycle.mm",
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
    installed_spec = (
        IOS_ROOT / "Pods/Local Podspecs/BleHubReactNativeBleng.podspec.json"
    )
    try:
        with installed_spec.open("rb") as stream:
            raw = stream.read(ios_pod_preparation.MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > ios_pod_preparation.MAX_SNAPSHOT_BYTES:
            raise BlengIOSConsumerError("installed formal BLeng podspec exceeds limit")
        spec = json.loads(raw)
    except (OSError, ValueError) as error:
        raise BlengIOSConsumerError(
            "missing or invalid installed formal BLeng podspec JSON; "
            "run explicit --refresh-pods"
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
    required_sources = (
        "ios/HybridBlengCentral.swift",
        *(f"ios/{name}" for name in LIFECYCLE_SOURCES),
    )
    missing_sources = [
        path
        for path in required_sources
        if not isinstance(source_files, list) or path not in source_files
    ]
    if missing_sources:
        raise BlengIOSConsumerError(
            "formal BLeng podspec does not expose required source entries: "
            + ", ".join(missing_sources)
        )


@contextmanager
def _build_lock() -> Generator[None, None, None]:
    """Serialize this owner's preparation and build; never adopt another writer."""
    BUILD_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(BUILD_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BlengIOSConsumerError(
                "formal iOS build owner is already active"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _pod_toolchain(environment: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    """Recognize installed Ruby pod launchers, including Homebrew's GEM_HOME shim.

    No shell evaluation, probing installs, or fallback to a mismatched system Ruby.
    Unknown launchers fail closed instead of guessing their effective gem context.
    """
    pod = Path(_require_executable("pod")).resolve(strict=True)
    effective = dict(environment)

    def header(path: Path) -> str:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        return raw.decode("utf-8") if len(raw) <= 8192 else raw[:8192].decode("utf-8")

    try:
        text = header(pod)
        if text.startswith("#!/bin/bash\n"):
            match = re.fullmatch(
                r'#!/bin/bash\nGEM_HOME="([^"\n]+)" exec "([^"\n]+)"  "\$@"\n?', text
            )
            if match is None:
                raise BlengIOSConsumerError("unsupported CocoaPods shell launcher")
            gem_home, script = match.groups()
            if not Path(gem_home).is_dir() or not Path(script).is_absolute():
                raise BlengIOSConsumerError("invalid CocoaPods gem launcher")
            effective["GEM_HOME"] = gem_home
            text = header(Path(script))
        shebang = text.splitlines()[0]
        if shebang == "#!/usr/bin/env ruby":
            ruby = shutil.which("ruby", path=effective.get("PATH"))
        elif re.fullmatch(r"#!/[^\s]+/ruby", shebang):
            ruby = shebang[2:]
        else:
            ruby = None
        if ruby is None or not os.access(ruby, os.X_OK):
            raise BlengIOSConsumerError("unsupported CocoaPods Ruby launcher")
        return str(pod), str(Path(ruby).resolve(strict=True)), effective
    except (OSError, UnicodeError, IndexError) as error:
        raise BlengIOSConsumerError(
            "cannot inspect installed CocoaPods toolchain"
        ) from error


def _prepare_pods(
    *,
    refresh_pods: bool,
    environment: dict[str, str],
    allow_podfile_change: bool = False,
) -> None:
    """Bind RN's two prebuilt inputs to the host-local cache when complete.

    RN 0.87 otherwise performs a remote HEAD before consulting its cached archive.
    Both archives must be present before either local override is admitted; a
    partial cache is not allowed to create a mixed graph.
    """
    effective = dict(environment)
    package_json = HOST_ROOT / "node_modules/react-native/package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            version = package["version"]
            if not isinstance(version, str) or not re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+].*)?", version
            ):
                raise ValueError
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise BlengIOSConsumerError(
                "cannot determine installed React Native version"
            ) from error
        core = (
            IOS_ROOT
            / f"Pods/ReactNativeCore-artifacts/reactnative-core-{version}-debug.tar.gz"
        )
        dependencies = (
            IOS_ROOT
            / f"Pods/ReactNativeDependencies-artifacts/reactnative-dependencies-{version}-debug.tar.gz"
        )
        if core.is_file() and dependencies.is_file():
            effective.update(
                RCT_USE_RN_DEP="1",
                RCT_USE_PREBUILT_RNCORE="1",
                RCT_USE_LOCAL_RN_DEP=str(dependencies),
                RCT_TESTONLY_RNCORE_TARBALL_PATH=str(core),
            )
    pod, ruby, effective = _pod_toolchain(effective)

    def snapshot(command: list[str], *, cwd: Path, timeout: int) -> str:
        if command[0] != "ruby":
            raise BlengIOSConsumerError("unexpected Pods snapshot command")
        return ios_pod_refresh.run_snapshot(
            [ruby, *command[1:]], cwd=cwd, timeout=timeout, environment=effective
        )

    context = {
        "ios_root": IOS_ROOT,
        "host_root": HOST_ROOT,
        "staging_root": RN_STAGED_ROOT,
        "run": snapshot,
        "environment": effective,
    }
    try:
        if not refresh_pods:
            ios_pod_preparation.verify_preparation(POD_RECEIPT, **context)
            print("bleng_pods_preparation=verified (no pod install)", flush=True)
            return
        before = ios_pod_preparation.begin_preparation(POD_RECEIPT, **context)
        POD_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Each explicit attempt retains its own raw private log, including failure.
        attempt = Path(tempfile.mkdtemp(prefix="bleng-pods-", dir=POD_LOG.parent))
        log = attempt / POD_LOG.name
        print(f"bleng_pods_refresh_log={log}", flush=True)
        with ios_pod_refresh.curl_environment(effective) as refresh_environment:
            # `--deployment` keeps a refresh from silently re-resolving: the lockfile must stay
            # put. A Podfile change is a different event and needs its own authorization, so the
            # flag is dropped only when the caller declared that change.
            command = [
                pod,
                "install",
                "--no-repo-update",
                "--clean-install",
                *([] if allow_podfile_change else ["--deployment"]),
            ]
            ios_pod_refresh.run_logged(
                command,
                cwd=IOS_ROOT,
                timeout=240,
                log_path=log,
                environment=refresh_environment,
            )
        ios_pod_preparation.seal_preparation(
            POD_RECEIPT, before=before, refresh_succeeded=True, **context
        )
        print(f"bleng_pods_preparation=refreshed receipt={POD_RECEIPT}", flush=True)
    except (
        ios_pod_preparation.PodPreparationError,
        ios_pod_refresh.PodRefreshError,
        builtins.ExceptionGroup,
    ) as error:
        detail = "".join(format_exception_only(error, show_group=True)).strip()
        raise BlengIOSConsumerError(detail) from error


def build(*, refresh_pods: bool = False, allow_podfile_change: bool = False) -> None:
    with _build_lock():
        _build(refresh_pods=refresh_pods, allow_podfile_change=allow_podfile_change)


def require_current_apple_backend() -> None:
    """Refuse a staged Apple backend that is not the source this checkout carries.

    The device and simulator products link `RN_STAGED_ROOT`, not `platforms/apple`, so a backend
    fix that was never staged builds and installs as the previous product — the failure then looks
    like a device defect. Content is compared rather than modification time, because staging
    preserves timestamps and a restored tree would look fresh.

    A tree with no staged backend sources at all is a synthetic build fixture, not a product this
    flow can sign; the real staging owner's required-file check covers that absence.
    """
    for source in sorted(APPLE_BACKEND_SOURCES.rglob("*.swift")):
        staged = (
            RN_STAGED_ROOT
            / APPLE_BACKEND_STAGED
            / source.relative_to(APPLE_BACKEND_SOURCES)
        )
        if staged.is_file() and staged.read_bytes() == source.read_bytes():
            continue
        # A tree staged by a synthetic harness has no backend sources at all; the missing-file
        # check belongs to the real staging owner (STAGED_FILES), not to this content comparison.
        if not staged.parent.is_dir():
            continue
        raise BlengIOSConsumerError(
            "the staged Apple backend does not match platforms/apple, so the product "
            "would not contain this source: "
            f"{source.relative_to(APPLE_BACKEND_SOURCES)}; run "
            "`pixi run just react-native stage-apple-bleng`"
        )


def _build(*, refresh_pods: bool, allow_podfile_change: bool = False) -> None:
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
    require_current_apple_backend()
    installed = (
        FORMAL_PODSPEC,
        FORMAL_PACKAGE_ROOT / "src/index.ts",
        RN_CLI,
        FORMAL_PACKAGE_ROOT / "ios/HybridBlengCentral.swift",
        *(
            FORMAL_PACKAGE_ROOT / "ios" / name
            for name in (*PLATFORM_SOURCES, *LIFECYCLE_SOURCES)
        ),
    )
    missing = [str(path) for path in installed if not path.is_file()]
    if missing:
        raise BlengIOSConsumerError(
            "formal installed package is incomplete; run "
            "`pixi run just react-native formal-host-install` first: "
            + ", ".join(missing)
        )
    required = (
        HOST_PROJECT,
        IOS_ROOT / "Podfile",
        IOS_ROOT / "BleHubBlengHost/AppDelegate.swift",
        IOS_ROOT / "BleHubBlengHost/FormalReactNativeDelegate.h",
        IOS_ROOT / "BleHubBlengHost/FormalReactNativeDelegate.mm",
        IOS_ROOT / "BleHubBlengHost/BleHubBlengHost-Bridging-Header.h",
        IOS_ROOT / "BleHubBlengHostNativeTests/BlengHostReloadHarness.h",
        IOS_ROOT / "BleHubBlengHostNativeTests/BlengHostReloadHarness.mm",
        NATIVE_TEST_SOURCE,
        NATIVE_TEST_ENTRY,
        HOST_ROOT / "metro.config.js",
        HOST_ROOT / "babel.config.js",
        SPEC,
        SMOKE_ROOT / "LifecycleCompileSmoke.swift",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BlengIOSConsumerError(
            "formal host/native contract inputs are incomplete: " + ", ".join(missing)
        )
    node = _require_executable("node")
    # Match the Podfile's override without leaking it into the controller process.
    # Ordinary builds never reach Podfile evaluation. Explicit refresh keeps the
    # reviewed prebuilt RN graph together; the Podfile rejects a transient mixed
    # prebuilt/source fallback instead of sealing an ambiguous installation.
    env = dict(
        os.environ,
        BLEHUB_APPLE_BLENG_STAGING_ROOT=str(RN_STAGED_ROOT),
        RCT_USE_RN_DEP="1",
        RCT_USE_PREBUILT_RNCORE="1",
    )
    _prepare_pods(
        refresh_pods=refresh_pods,
        environment=env,
        allow_podfile_change=allow_podfile_change,
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

    if not WORKSPACE.is_dir():
        raise BlengIOSConsumerError(
            f"CocoaPods did not produce the formal host workspace: {WORKSPACE}"
        )
    # A successful command that emits nothing must not reuse an earlier bundle.
    NATIVE_TEST_BUNDLE.unlink(missing_ok=True)
    _run(
        [
            node,
            str(RN_CLI),
            "bundle",
            "--config",
            "metro.config.js",
            "--platform",
            "ios",
            "--dev",
            "false",
            "--minify",
            "false",
            "--max-workers",
            "1",
            "--entry-file",
            str(NATIVE_TEST_ENTRY.relative_to(HOST_ROOT)),
            "--bundle-output",
            str(NATIVE_TEST_BUNDLE.relative_to(HOST_ROOT)),
            "--assets-dest",
            str(NATIVE_TEST_BUNDLE.parent.relative_to(HOST_ROOT)),
        ],
        cwd=HOST_ROOT,
        log=BUNDLE_LOG,
        timeout=180,
    )
    if not NATIVE_TEST_BUNDLE.is_file() or NATIVE_TEST_BUNDLE.stat().st_size == 0:
        raise BlengIOSConsumerError(
            f"missing or empty native-test JS bundle: {NATIVE_TEST_BUNDLE}"
        )
    bundle_sha256 = hashlib.sha256(NATIVE_TEST_BUNDLE.read_bytes()).digest()
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
    resource = products / "BleHubBlengHostNativeTests.xctest" / NATIVE_TEST_BUNDLE.name
    if not resource.is_file() or resource.stat().st_size == 0:
        raise BlengIOSConsumerError(
            f"missing or empty bundled native-test JS resource: {resource}"
        )
    if hashlib.sha256(resource.read_bytes()).digest() != bundle_sha256:
        raise BlengIOSConsumerError(
            f"native-test JS resource SHA256 mismatch: {resource}"
        )
    print(f"bleng_platform_contract_product={PRODUCT}", flush=True)
    print(f"bleng_formal_host_native_test_derived_data={HOST_DERIVED_DATA}", flush=True)


def signed_bundle_id(signing: dict) -> str:
    """The bundle id this App signs as, derived from the profile it signs with.

    A provisioning profile authorises a pattern, not a name. A Team wildcard authorises
    `<team>.*`, so the App's own id must extend that pattern; an explicit profile
    authorises exactly one id, so that id is the only choice. Deriving it this way means
    the same code signs correctly under either profile shape instead of assuming one.
    """
    pattern = signing["application_identifier"]
    if pattern.endswith("*"):
        return pattern[:-1] + DEVICE_BUNDLE_SUFFIX
    return pattern


def build_device(
    *, device: str, refresh_pods: bool = False, allow_podfile_change: bool = False
) -> Path:
    """Produce one signed physical-device App for this device's own profile.

    The simulator build deliberately signs nothing; a device App cannot exist unsigned,
    so this path resolves the local Team, identity and profile (read-only, see
    `ios_preflight.development_signing`) and signs with exactly those. It never requests
    a provisioning update, registers a device or touches an Apple account: everything it
    uses is already on this Mac, and a keychain that refuses private-key access fails the
    build rather than being worked around.

    The result is verified by the same read-only preflight a prebuilt App goes through,
    so a build that produced a wrongly-signed or wrongly-profiled App is reported here
    instead of at install time.
    """
    build(refresh_pods=refresh_pods, allow_podfile_change=allow_podfile_change)
    signing = ios_preflight.development_signing(device)
    bundle_id = signed_bundle_id(signing)
    xcodebuild = _require_executable("xcodebuild")
    command = [
        xcodebuild,
        "-workspace",
        str(WORKSPACE),
        "-scheme",
        HOST_SCHEME,
        "-configuration",
        "Release",
        "-sdk",
        "iphoneos",
        "-destination",
        f"platform=iOS,id={signing['device']}",
        "-derivedDataPath",
        str(DEVICE_DERIVED_DATA),
        # NOTE: no `-allowProvisioningDeviceRegistration`. The resolved profile already
        # lists this device, so registration has nothing to do; a build that needs it is a
        # build whose profile does not cover the device, and the resolver refuses that case
        # by name. (The hang observed here was not caused by this flag: it was `codesign`
        # blocking on private-key access, which is a human keychain step.)
        #
        # A profile's own shape decides how it may be consumed, so the build follows what
        # the resolver reported instead of assuming one kind:
        #   Xcode-managed  → automatic signing, which resolves it locally. It deliberately
        #                    does NOT pass `-allowProvisioningUpdates`, so nothing is
        #                    created, refreshed or requested from Apple. Automatic signing
        #                    also requires the generic identity name, not a specific one;
        #                    the team selects the identity, and the resolver already proved
        #                    the profile carries it.
        #   manual         → manual signing naming that profile and identity explicitly.
        # Either way, insufficient local material fails the build instead of reaching for
        # an account.
        *(
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
        ),
        f"PRODUCT_BUNDLE_IDENTIFIER={bundle_id}",
        "ONLY_ACTIVE_ARCH=YES",
        "build",
    ]
    env = dict(
        os.environ,
        BLEHUB_APPLE_BLENG_STAGING_ROOT=str(RN_STAGED_ROOT),
        RCT_USE_RN_DEP="1",
        RCT_USE_PREBUILT_RNCORE="1",
    )
    consumer.run_signed_build(
        command,
        DEVICE_LOG,
        env=env,
        cwd=IOS_ROOT,
        # A device Release build compiles the whole React Native graph for a different
        # SDK and architecture, which does not fit the 600 s a small probe App needs.
        # The bound is still finite: an interrupted build resumes incrementally.
        timeout=DEVICE_BUILD_SECONDS,
    )
    products = DEVICE_DERIVED_DATA / "Build/Products/Release-iphoneos"
    app = products / f"{HOST_SCHEME}.app"
    executable = app / HOST_SCHEME
    if not executable.is_file() or executable.stat().st_size == 0:
        raise BlengIOSConsumerError(
            f"missing signed device executable: {executable}; see {DEVICE_LOG}"
        )
    if not (app / "embedded.mobileprovision").is_file():
        raise BlengIOSConsumerError(
            f"signed device App carries no provisioning profile: {app}"
        )
    # The preflight is the single owner of "is this App actually installable", so the
    # build does not re-implement signature, entitlement or profile checks.
    ios_preflight.require_prebuilt(app, device=signing["device"])
    print(f"bleng_formal_host_device_app={app}", flush=True)
    print(f"bleng_formal_host_device_bundle_id={bundle_id}", flush=True)
    print(f"bleng_formal_host_device_signed_by={signing['identity']}", flush=True)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-pods",
        action="store_true",
        help="explicit bounded Pods refresh; ordinary builds only verify local preparation",
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
        "--device",
        help=(
            "build a signed App for this physical UDID instead of the unsigned simulator "
            "one; requires local Team/identity/profile material and no Apple account"
        ),
    )
    args = parser.parse_args(argv)
    try:
        if args.device is None:
            build(
                refresh_pods=args.refresh_pods,
                allow_podfile_change=args.accept_podfile_change,
            )
        else:
            build_device(
                device=args.device,
                refresh_pods=args.refresh_pods,
                allow_podfile_change=args.accept_podfile_change,
            )
    except BlengIOSConsumerError as error:
        parser.exit(1, f"formal iOS consumer: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
