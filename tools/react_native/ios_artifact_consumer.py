"""Build an optional clean, artifact-only React Native iOS consumer.

This runner deliberately does not use the repository's React Native host as a
working directory.  It first creates a small package staging tree, packs that
tree, and then installs the tarball into a second clean host tree.  The final
The default build is unsigned and simulator/device-link-only. An explicit device UDID and
Development Team opt into a separate signed iPhone runtime smoke; this module never archives
or runs RF.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tools.react_native.host_contract import (
    APP_NAME,
    BUNDLE_ID,
    SCHEME,
    UI_TEST_TARGET,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = PROJECT_ROOT / "integrations/react-native-internal"
APPLE_STAGED_ROOT = PROJECT_ROOT / ".work" / "staged" / "apple-production-react-native"
HOST_SOURCE_ROOT = PROJECT_ROOT / "tests/consumers/react-native-internal"
WORK_ROOT = PROJECT_ROOT / ".work/react-native-ios-artifact-consumer"
PACKAGE_NAME = "@blehub/react-native-internal"
PACKAGE_NODE_NAME = "@blehub/react-native-internal"
TAIL_LINES = 80

# These are intentionally explicit.  In particular, neither Android sources,
# package node_modules, nor generated/build output is allowed to enter npm pack.
INTEGRATION_FILES: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "BleHubReactNative.podspec",
    "react-native.config.js",
    "src",
    "specs",
    "ios/BoundedEventHandoff.swift",
    "ios/HybridBleHubProbe.swift",
    "nitrogen/generated/ios",
    "nitrogen/generated/shared",
)
APPLE_POD_FILES: tuple[str, ...] = (
    "BleHubApple/BleHubApple.podspec",
    "BleHubApple/Sources",
    "BleHubUniFfi/BleHubUniFfi.podspec",
    "BleHubUniFfi/Sources",
    "BleHubUniFfi/BleHubUniFfiNative.xcframework",
)
HOST_FILES: tuple[str, ...] = (
    ".eslintrc.js",
    ".prettierrc.js",
    ".watchmanconfig",
    "app.json",
    "babel.config.js",
    "index.js",
    "jest.config.js",
    "package-lock.json",
    "package.json",
    "react-native.config.js",
    "src",
    "ios/.xcode.env",
    "ios/BleHubNitroHost",
    "ios/BleHubNitroHost.xcodeproj",
    "ios/BleHubNitroHostUITests",
)

METRO_CONFIG = """const { getDefaultConfig } = require('@react-native/metro-config');

module.exports = getDefaultConfig(__dirname);
"""

RN_CONFIG = """const path = require('node:path');

const integrationRoot = path.dirname(
  require.resolve('@blehub/react-native-internal/package.json'),
);

module.exports = {
  dependencies: {
    '@blehub/react-native-internal': {
      root: integrationRoot,
    },
  },
};
"""

PODFILE = """# Resolve both React Native and the private integration from installed packages.
react_native_package = Pod::Executable.execute_command('node', ['-p',
  'require.resolve("react-native/package.json", {paths: [process.argv[1]]})', __dir__]).strip
require File.join(File.dirname(react_native_package), 'scripts', 'react_native_pods.rb')

integration_package = Pod::Executable.execute_command('node', ['-p',
  'require.resolve("@blehub/react-native-internal/package.json", {paths: [process.argv[1]]})', __dir__]).strip
integration_root = File.dirname(integration_package)

platform :ios, '16.0'
prepare_react_native_project!

linkage = ENV['USE_FRAMEWORKS']
if linkage != nil
  Pod::UI.puts "Configuring Pod with #{linkage}ally linked Frameworks".green
  use_frameworks! :linkage => linkage.to_sym
end

target 'BleHubNitroHost' do
  config = use_native_modules!

  pod 'BleHubUniFfi', :path => File.join(integration_root, 'apple-pods', 'BleHubUniFfi')
  pod 'BleHubApple', :path => File.join(integration_root, 'apple-pods', 'BleHubApple')

  use_react_native!(
    :path => config[:reactNativePath],
    :app_path => "#{Pod::Config.instance.installation_root}/.."
  )

  post_install do |installer|
    react_native_post_install(
      installer,
      config[:reactNativePath],
      :mac_catalyst_enabled => false
    )
  end
end
"""


class ArtifactConsumerError(RuntimeError):
    """Raised when the clean artifact boundary cannot be proven."""


@dataclass(frozen=True)
class Layout:
    root: Path
    package_root: Path
    artifact_root: Path
    host_root: Path
    log_root: Path

    @property
    def tarball(self) -> Path:
        matches = sorted(self.artifact_root.glob("*.tgz"))
        if len(matches) != 1:
            raise ArtifactConsumerError(
                f"expected exactly one npm tarball in {self.artifact_root}, found {matches}"
            )
        return matches[0]

    @property
    def simulator_derived_data(self) -> Path:
        return self.host_root / "ios/build/simulator-derived"

    @property
    def simulator_binary(self) -> Path:
        return (
            self.simulator_derived_data
            / "Build/Products/Debug-iphonesimulator"
            / f"{SCHEME}.app/{SCHEME}.debug.dylib"
        )

    @property
    def device_derived_data(self) -> Path:
        return self.host_root / "ios/build/device-derived"

    @property
    def device_binary(self) -> Path:
        return (
            self.device_derived_data
            / "Build/Products/Debug-iphoneos"
            / f"{SCHEME}.app/{SCHEME}.debug.dylib"
        )


def make_layout(root: Path = WORK_ROOT) -> Layout:
    return Layout(
        root=root,
        package_root=root / "package",
        artifact_root=root / "artifacts",
        host_root=root / "host",
        log_root=root / "logs",
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ArtifactConsumerError(f"missing required executable: {name}")
    return executable


def _copy_checked(source: Path, destination: Path) -> None:
    """Copy one whitelist entry while rejecting symlink-based source escape."""
    if source.is_symlink():
        raise ArtifactConsumerError(f"refusing symlinked source entry: {source}")
    if not source.exists():
        raise ArtifactConsumerError(f"missing whitelisted source entry: {source}")

    if source.is_dir():
        for child in source.rglob("*"):
            if child.is_symlink():
                raise ArtifactConsumerError(f"refusing symlink in source tree: {child}")
        shutil.copytree(source, destination, symlinks=False)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise ArtifactConsumerError(f"unsupported source entry: {source}")


def _copy_whitelist(
    source_root: Path, destination_root: Path, entries: Iterable[str]
) -> None:
    for relative_name in entries:
        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactConsumerError(
                f"invalid internal whitelist entry: {relative_name}"
            )
        _copy_checked(source_root / relative, destination_root / relative)


def _prepare_work_tree(layout: Layout) -> None:
    resolved_root = layout.root.resolve()
    resolved_project = PROJECT_ROOT.resolve()
    if not _is_relative_to(resolved_root, resolved_project / ".work"):
        raise ArtifactConsumerError(f"work tree must be below {PROJECT_ROOT / '.work'}")
    if layout.root == resolved_project / ".work":
        raise ArtifactConsumerError("refusing to use the .work directory itself")
    if layout.root.exists() or layout.root.is_symlink():
        raise ArtifactConsumerError(
            f"refusing to reuse existing artifact-consumer run directory: {layout.root}"
        )
    layout.root.mkdir(parents=True, exist_ok=False)
    layout.package_root.mkdir()
    layout.artifact_root.mkdir()
    layout.log_root.mkdir()


def _new_run_root(parent: Path = WORK_ROOT) -> Path:
    return parent / f"run-{uuid.uuid4().hex}"


def stage_package(
    layout: Layout,
    *,
    integration_root: Path = INTEGRATION_ROOT,
    apple_pods_root: Path = APPLE_STAGED_ROOT,
) -> None:
    """Create the package tree from the reviewed integration/artifact whitelist."""
    _copy_whitelist(integration_root, layout.package_root, INTEGRATION_FILES)
    _copy_whitelist(
        apple_pods_root, layout.package_root / "apple-pods", APPLE_POD_FILES
    )

    required = (
        layout.package_root / "package.json",
        layout.package_root / "BleHubReactNative.podspec",
        layout.package_root / "nitrogen/generated/ios/BleHubReactNative+autolinking.rb",
        layout.package_root / "nitrogen/generated/shared/c++/HybridBleHubProbeSpec.hpp",
        layout.package_root / "apple-pods/BleHubApple/Sources/BleHubApple",
        layout.package_root
        / "apple-pods/BleHubUniFfi/Sources/BleHubUniFfi/blehub.swift",
        layout.package_root / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ArtifactConsumerError(f"staged package is incomplete: {missing}")

    _assert_no_symlinks(layout.package_root)
    _assert_tree_sha256_equal(
        apple_pods_root / "BleHubUniFfi/BleHubUniFfiNative.xcframework",
        layout.package_root / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework",
        "source versus staged XCFramework",
    )
    _assert_no_checkout_leakage(layout.package_root)


def _assert_no_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArtifactConsumerError(f"symlink in disposable tree: {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise ArtifactConsumerError(f"unable to hash {path}: {error}") from error
    return digest.hexdigest()


def _sha256_tree(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise ArtifactConsumerError(f"cannot hash non-directory artifact tree: {root}")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArtifactConsumerError(f"symlink in artifact tree: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256_file(path)
    return result


def _assert_tree_sha256_equal(expected: Path, actual: Path, description: str) -> None:
    expected_hashes = _sha256_tree(expected)
    actual_hashes = _sha256_tree(actual)
    if expected_hashes != actual_hashes:
        missing = sorted(set(expected_hashes) - set(actual_hashes))
        unexpected = sorted(set(actual_hashes) - set(expected_hashes))
        changed = sorted(
            name
            for name in set(expected_hashes) & set(actual_hashes)
            if expected_hashes[name] != actual_hashes[name]
        )
        raise ArtifactConsumerError(
            f"{description} SHA-256 mismatch; missing={missing}, "
            f"unexpected={unexpected}, changed={changed}"
        )


def _run_quiet(
    command: Sequence[str], *, cwd: Path, log: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    print(
        f"[rn-artifact] running {Path(command[0]).name}; full log: {log}",
        flush=True,
    )
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        output = f"command timed out after {timeout:g}s\n{stdout}{stderr}"
        log.write_text(output, encoding="utf-8")
        raise ArtifactConsumerError(
            f"command timed out after {timeout:g}s: {command[0]} (full log: {log})"
        ) from error
    log.write_text(output, encoding="utf-8")
    if result.returncode != 0:
        tail = "\n".join(output.splitlines()[-TAIL_LINES:])
        raise ArtifactConsumerError(
            f"command failed with exit {result.returncode}: {command[0]}\n"
            f"{tail}\nfull log: {log}"
        )
    return result


def pack_package(layout: Layout) -> Path:
    """Run npm pack only from the small staged package, never from checkout source."""
    npm = _require_executable("npm")
    result = _run_quiet(
        [
            npm,
            "pack",
            "--ignore-scripts",
            "--json",
            "--pack-destination",
            str(layout.artifact_root),
        ],
        cwd=layout.package_root,
        log=layout.log_root / "npm-pack.log",
        timeout=120,
    )
    try:
        metadata_value = json.loads(result.stdout.strip())
        metadata = (
            metadata_value[0] if isinstance(metadata_value, list) else metadata_value
        )
        filename = metadata["filename"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ArtifactConsumerError(
            f"npm pack did not return a usable tarball filename (full log: "
            f"{layout.log_root / 'npm-pack.log'})"
        ) from error
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ArtifactConsumerError(
            f"npm returned unsafe tarball filename: {filename!r}"
        )
    tarball = layout.artifact_root / filename
    if not tarball.is_file() or tarball.is_symlink():
        raise ArtifactConsumerError(f"npm pack did not create {tarball}")
    return tarball


def _forbidden_checkout_tokens() -> tuple[bytes, ...]:
    root = PROJECT_ROOT.resolve()
    return tuple(
        value.encode()
        for value in {
            str(root),
            root.as_posix(),
            "integrations/react-native-internal",
            "tests/consumers/react-native-internal",
        }
    )


TEXTUAL_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".js",
        ".json",
        ".lock",
        ".md",
        ".mm",
        ".plist",
        ".podspec",
        ".rb",
        ".swift",
        ".ts",
        ".tsx",
        ".txt",
        ".yml",
        ".yaml",
    }
)
TEXTUAL_NAMES = frozenset({"Podfile", "Gemfile", ".xcode.env"})


def _is_textual_path(path: Path) -> bool:
    return path.name in TEXTUAL_NAMES or path.suffix in TEXTUAL_SUFFIXES


def _assert_no_checkout_leakage(root: Path) -> None:
    """Scan textual package/config files, not opaque native binaries."""
    tokens = _forbidden_checkout_tokens()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArtifactConsumerError(f"symlink escaped into clean tree: {path}")
        if not path.is_file() or not _is_textual_path(path):
            continue
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ArtifactConsumerError(f"unable to inspect {path}: {error}") from error
        for token in tokens:
            if token and token in data:
                raise ArtifactConsumerError(f"checkout path leakage in {path}")


def verify_tarball(
    tarball: Path, *, source_xcframework: Path, staged_xcframework: Path
) -> None:
    """Check textual isolation and exact native provenance through npm pack."""
    _assert_tree_sha256_equal(
        source_xcframework, staged_xcframework, "source versus staged XCFramework"
    )
    expected_hashes = _sha256_tree(staged_xcframework)
    archive_hashes: dict[str, str] = {}
    xcframework_prefix = (
        "package/apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework/"
    )
    required_prefixes = (
        "package/nitrogen/generated/ios/",
        "package/nitrogen/generated/shared/",
        "package/apple-pods/BleHubApple/Sources/",
        "package/apple-pods/BleHubUniFfi/Sources/",
        "package/apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework/",
    )
    try:
        with tarfile.open(tarball, mode="r:gz") as archive:
            names: list[str] = []
            seen: set[str] = set()
            tokens = _forbidden_checkout_tokens()
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not name.parts
                    or name.parts[0] != "package"
                    or member.name in seen
                ):
                    raise ArtifactConsumerError(
                        f"unsafe npm tarball entry: {member.name}"
                    )
                seen.add(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ArtifactConsumerError(
                        f"unsupported npm tarball entry: {member.name}"
                    )
                names.append(member.name)
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArtifactConsumerError(
                            f"unable to read npm entry: {member.name}"
                        )
                    if member.name.startswith(xcframework_prefix):
                        relative = member.name.removeprefix(xcframework_prefix)
                        digest = hashlib.sha256()
                        textual_parts: list[bytes] = []
                        for block in iter(stream.read, b""):
                            digest.update(block)
                            if _is_textual_path(Path(relative)):
                                textual_parts.append(block)
                        archive_hashes[relative] = digest.hexdigest()
                        if any(token in b"".join(textual_parts) for token in tokens):
                            raise ArtifactConsumerError(
                                f"checkout path leakage in npm tarball entry: {member.name}"
                            )
                    elif _is_textual_path(Path(member.name)):
                        data = stream.read()
                        if any(token in data for token in tokens):
                            raise ArtifactConsumerError(
                                f"checkout path leakage in npm tarball entry: {member.name}"
                            )
    except (OSError, tarfile.TarError) as error:
        raise ArtifactConsumerError(
            f"unable to inspect npm tarball {tarball}: {error}"
        ) from error

    for prefix in required_prefixes:
        if not any(name.startswith(prefix) for name in names):
            raise ArtifactConsumerError(
                f"npm tarball is missing required surface: {prefix}"
            )
    if (
        "package/package.json" not in names
        or "package/BleHubReactNative.podspec" not in names
    ):
        raise ArtifactConsumerError(
            "npm tarball is missing the package manifest or podspec"
        )
    if archive_hashes != expected_hashes:
        missing = sorted(set(expected_hashes) - set(archive_hashes))
        unexpected = sorted(set(archive_hashes) - set(expected_hashes))
        changed = sorted(
            name
            for name in set(expected_hashes) & set(archive_hashes)
            if expected_hashes[name] != archive_hashes[name]
        )
        raise ArtifactConsumerError(
            f"staged versus npm tarball XCFramework SHA-256 mismatch; "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactConsumerError(f"invalid JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise ArtifactConsumerError(f"JSON root is not an object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _rewrite_host_package(host_root: Path, tarball: Path) -> None:
    package_path = host_root / "package.json"
    package = _read_json(package_path)
    dependencies = package.get("dependencies")
    if not isinstance(dependencies, dict) or PACKAGE_NAME not in dependencies:
        raise ArtifactConsumerError(f"host package has no {PACKAGE_NAME} dependency")
    relative_tarball = os.path.relpath(tarball, host_root).replace(os.sep, "/")
    dependencies[PACKAGE_NAME] = f"file:{relative_tarball}"
    _write_json(package_path, package)


def _rewrite_host_lockfile(host_root: Path, tarball: Path) -> None:
    lock_path = host_root / "package-lock.json"
    lock = _read_json(lock_path)
    relative_tarball = os.path.relpath(tarball, host_root).replace(os.sep, "/")
    packages_value = lock.get("packages")
    if not isinstance(packages_value, dict):
        raise ArtifactConsumerError("host package-lock.json has no packages object")
    root_package = packages_value.get("")
    if not isinstance(root_package, dict):
        raise ArtifactConsumerError("host package-lock.json has no root package")
    root_dependencies = root_package.get("dependencies")
    if not isinstance(root_dependencies, dict) or PACKAGE_NAME not in root_dependencies:
        raise ArtifactConsumerError(f"host lockfile has no {PACKAGE_NAME} dependency")
    root_dependencies[PACKAGE_NAME] = f"file:{relative_tarball}"

    for key in list(packages_value):
        if (
            "integrations/react-native-internal" in key
            or key == f"node_modules/{PACKAGE_NODE_NAME}"
        ):
            del packages_value[key]
    _write_json(lock_path, lock)


def _rewrite_tsconfig(host_root: Path, source_root: Path) -> None:
    config = _read_json(source_root / "tsconfig.json")
    compiler_options = config.get("compilerOptions")
    if not isinstance(compiler_options, dict):
        raise ArtifactConsumerError("host tsconfig has no compilerOptions object")
    # The installed package must be resolved by normal Node/TypeScript package
    # resolution.  Keeping either of these fields would point back to checkout.
    compiler_options.pop("paths", None)
    compiler_options.pop("baseUrl", None)
    compiler_options.pop("typeRoots", None)
    _write_json(host_root / "tsconfig.json", config)


def _write_clean_host_configs(host_root: Path, source_root: Path) -> None:
    (host_root / "metro.config.js").write_text(METRO_CONFIG, encoding="utf-8")
    (host_root / "react-native.config.js").write_text(RN_CONFIG, encoding="utf-8")
    (host_root / "ios/Podfile").write_text(PODFILE, encoding="utf-8")
    _rewrite_tsconfig(host_root, source_root)


def create_host(
    layout: Layout,
    tarball: Path,
    *,
    source_root: Path = HOST_SOURCE_ROOT,
) -> None:
    """Copy the host whitelist and rewrite only the disposable copy."""
    _copy_whitelist(source_root, layout.host_root, HOST_FILES)
    _rewrite_host_package(layout.host_root, tarball)
    _rewrite_host_lockfile(layout.host_root, tarball)
    _write_clean_host_configs(layout.host_root, source_root)
    _assert_no_symlinks(layout.host_root)
    _assert_no_checkout_leakage(layout.host_root)


def install_dependencies(layout: Layout) -> None:
    npm = _require_executable("npm")
    _run_quiet(
        [npm, "install", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=layout.host_root,
        log=layout.log_root / "npm-install.log",
        timeout=600,
    )
    installed = layout.host_root / "node_modules" / PACKAGE_NODE_NAME
    if not installed.is_dir() or installed.is_symlink():
        raise ArtifactConsumerError(
            f"npm did not install a real package directory: {installed}"
        )
    if not _is_relative_to(installed.resolve(), layout.host_root.resolve()):
        raise ArtifactConsumerError(
            f"installed package escaped clean host: {installed.resolve()}"
        )
    _assert_no_checkout_leakage(installed)

    _run_quiet(
        [
            str(layout.host_root / "node_modules/.bin/tsc"),
            "-p",
            "tsconfig.json",
            "--noEmit",
        ],
        cwd=layout.host_root,
        log=layout.log_root / "typescript-check.log",
        timeout=120,
    )
    config_result = _run_quiet(
        [
            str(layout.host_root / "node_modules/.bin/react-native"),
            "config",
            "--platform",
            "ios",
        ],
        cwd=layout.host_root,
        log=layout.log_root / "rn-ios-config.log",
        timeout=120,
    )
    try:
        config = json.loads(config_result.stdout)
        dependency = config["dependencies"][PACKAGE_NAME]
        root = Path(dependency["root"])
        podspec = Path(dependency["platforms"]["ios"]["podspecPath"])
    except (ValueError, KeyError, TypeError) as error:
        raise ArtifactConsumerError(
            "RN CLI did not resolve the installed iOS module"
        ) from error
    if root != installed or podspec != installed / "BleHubReactNative.podspec":
        raise ArtifactConsumerError(
            f"RN CLI resolved outside the installed artifact: root={root}, podspec={podspec}"
        )


FORCE_LOAD_PATTERN = re.compile(r'-force_load\s+(?:"([^"]+)"|(\S+))')
SDK_FORCE_LOADS = {
    "iphonesimulator": "ios-arm64_x86_64-simulator/libblehub_uniffi.a",
    "iphoneos": "ios-arm64/libblehub_uniffi.a",
}


def _assert_pod_linker_provenance(layout: Layout, *, source_xcframework: Path) -> None:
    """Verify CocoaPods retained exactly the installed package's native inputs."""
    config_path = (
        layout.host_root
        / "ios/Pods/Target Support Files/Pods-BleHubNitroHost/"
        / "Pods-BleHubNitroHost.debug.xcconfig"
    )
    if not config_path.is_file() or config_path.is_symlink():
        raise ArtifactConsumerError(f"missing CocoaPods debug xcconfig: {config_path}")
    try:
        config = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ArtifactConsumerError(
            f"unable to read CocoaPods xcconfig {config_path}: {error}"
        ) from error

    installed_xcframework = (
        layout.host_root
        / "node_modules/@blehub/react-native-internal/apple-pods/"
        / "BleHubUniFfi/BleHubUniFfiNative.xcframework"
    )
    forbidden_paths = {
        str(INTEGRATION_ROOT),
        str(layout.package_root),
        "integrations/react-native-internal",
        "staged/apple-production",
    }
    leaked = sorted(path for path in forbidden_paths if path in config)
    if leaked:
        raise ArtifactConsumerError(
            f"CocoaPods xcconfig contains forbidden checkout/staged paths: {leaked}"
        )

    for sdk, relative_archive in SDK_FORCE_LOADS.items():
        expected_archive = installed_xcframework / relative_archive
        source_archive = source_xcframework / relative_archive
        if not expected_archive.is_file() or expected_archive.is_symlink():
            raise ArtifactConsumerError(
                f"missing installed native archive: {expected_archive}"
            )
        if not source_archive.is_file() or source_archive.is_symlink():
            raise ArtifactConsumerError(
                f"missing source native archive: {source_archive}"
            )
        if _sha256_file(source_archive) != _sha256_file(expected_archive):
            raise ArtifactConsumerError(
                f"installed native archive SHA-256 mismatch for {sdk}: {expected_archive}"
            )

        line_pattern = re.compile(
            rf"^OTHER_LDFLAGS\[sdk={re.escape(sdk)}\*\]\s*=\s*(.*?)\s*$",
            re.MULTILINE,
        )
        matches = line_pattern.findall(config)
        if len(matches) != 1:
            raise ArtifactConsumerError(
                f"expected exactly one {sdk} OTHER_LDFLAGS in {config_path}, found {len(matches)}"
            )
        force_loads = [
            quoted or bare for quoted, bare in FORCE_LOAD_PATTERN.findall(matches[0])
        ]
        expected_text = str(expected_archive)
        if force_loads.count(expected_text) != 1:
            raise ArtifactConsumerError(
                f"{sdk} xcconfig must contain exactly one expected -force_load: "
                f"expected={expected_text}, found={force_loads}"
            )
        if len(force_loads) != 1:
            raise ArtifactConsumerError(
                f"{sdk} xcconfig contains duplicate or unexpected -force_load flags: {force_loads}"
            )

    all_force_loads = [
        quoted or bare for quoted, bare in FORCE_LOAD_PATTERN.findall(config)
    ]
    if len(all_force_loads) != len(SDK_FORCE_LOADS):
        raise ArtifactConsumerError(
            f"unexpected additional or duplicate -force_load in {config_path}: {all_force_loads}"
        )


def pod_install(layout: Layout) -> None:
    pod = _require_executable("pod")
    _run_quiet(
        [pod, "install", "--no-repo-update"],
        cwd=layout.host_root / "ios",
        log=layout.log_root / "pod-install.log",
        timeout=600,
    )
    local_xcode_env = layout.host_root / "ios/.xcode.env.local"
    local_xcode_env.write_text(
        "export NODE_BINARY=$(command -v node)\n", encoding="utf-8"
    )
    if any(
        token in local_xcode_env.read_bytes() for token in _forbidden_checkout_tokens()
    ):
        raise ArtifactConsumerError(
            f"clean host local Xcode environment leaks checkout path: {local_xcode_env}"
        )
    _assert_pod_linker_provenance(
        layout,
        source_xcframework=APPLE_STAGED_ROOT
        / "BleHubUniFfi/BleHubUniFfiNative.xcframework",
    )


def simulator_build_command(layout: Layout) -> list[str]:
    xcodebuild = _require_executable("xcodebuild")
    workspace = layout.host_root / f"ios/{SCHEME}.xcworkspace"
    return [
        xcodebuild,
        "-quiet",
        "-workspace",
        str(workspace),
        "-scheme",
        SCHEME,
        "-configuration",
        "Debug",
        "-sdk",
        "iphonesimulator",
        "-destination",
        "generic/platform=iOS Simulator",
        "-derivedDataPath",
        str(layout.simulator_derived_data),
        "CODE_SIGNING_ALLOWED=NO",
        "CODE_SIGNING_REQUIRED=NO",
        "build",
    ]


def device_build_command(layout: Layout) -> list[str]:
    command = simulator_build_command(layout)
    command[command.index("iphonesimulator")] = "iphoneos"
    command[command.index("generic/platform=iOS Simulator")] = "generic/platform=iOS"
    command[command.index(str(layout.simulator_derived_data))] = str(
        layout.device_derived_data
    )
    return command


def signed_device_build_command(layout: Layout, udid: str, team: str) -> list[str]:
    if not re.fullmatch(r"[0-9A-Fa-f-]{25,40}", udid):
        raise ArtifactConsumerError(
            "device UDID must be an explicit physical identifier"
        )
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ArtifactConsumerError(
            "Development Team must be a 10-character Apple team ID"
        )
    command = device_build_command(layout)
    command[command.index("generic/platform=iOS")] = f"id={udid}"
    command.remove("CODE_SIGNING_ALLOWED=NO")
    command.remove("CODE_SIGNING_REQUIRED=NO")
    command.extend(
        [
            f"DEVELOPMENT_TEAM={team}",
            f"PRODUCT_BUNDLE_IDENTIFIER={BUNDLE_ID}",
            "CODE_SIGN_STYLE=Automatic",
            "CODE_SIGN_IDENTITY=Apple Development",
            "ONLY_ACTIVE_ARCH=YES",
        ]
    )
    return command


def ui_test_command(layout: Layout, udid: str, team: str) -> list[str]:
    if not re.fullmatch(r"[0-9A-Fa-f-]{25,40}", udid):
        raise ArtifactConsumerError(
            "UI test UDID must be an explicit physical identifier"
        )
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ArtifactConsumerError(
            "UI test Development Team must be a 10-character Apple team ID"
        )
    return [
        _require_executable("xcodebuild"),
        "-quiet",
        "-workspace",
        str(layout.host_root / f"ios/{SCHEME}.xcworkspace"),
        "-scheme",
        SCHEME,
        "-configuration",
        "Debug",
        "-destination",
        f"id={udid}",
        "-derivedDataPath",
        str(layout.host_root / "ios/build/ui-test-derived"),
        f"DEVELOPMENT_TEAM={team}",
        f"PRODUCT_BUNDLE_IDENTIFIER={BUNDLE_ID}",
        "CODE_SIGN_STYLE=Automatic",
        "CODE_SIGN_IDENTITY=Apple Development",
        "ONLY_ACTIVE_ARCH=YES",
        f"-only-testing:{UI_TEST_TARGET}",
        "test",
    ]


def build_and_check(
    layout,
    *,
    device: tuple[str, str] | None = None,
    ui_test: bool = False,
) -> None:
    if device is None:
        for label, command, binary in (
            ("simulator", simulator_build_command(layout), layout.simulator_binary),
            ("device", device_build_command(layout), layout.device_binary),
        ):
            _run_quiet(
                command,
                cwd=layout.host_root / "ios",
                log=layout.log_root / f"{label}-build.log",
                timeout=900,
            )
            if not binary.is_file():
                raise ArtifactConsumerError(
                    f"unsigned {label} build did not produce explicit product: {binary}"
                )
            _run_quiet(
                [
                    sys.executable,
                    "-m",
                    "tools.react_native.check_single_image",
                    "ios",
                    str(binary),
                ],
                cwd=PROJECT_ROOT,
                log=layout.log_root / f"{label}-single-image-check.log",
                timeout=120,
            )
        return

    udid, team = device
    _run_quiet(
        signed_device_build_command(layout, udid, team),
        cwd=layout.host_root / "ios",
        log=layout.log_root / "signed-device-build.log",
        timeout=900,
    )
    signed_app = layout.device_derived_data / "Build/Products/Debug-iphoneos" / APP_NAME
    if (
        not signed_app.is_dir()
        or not (signed_app / "embedded.mobileprovision").is_file()
    ):
        raise ArtifactConsumerError(
            f"signed device build did not produce a provisioned app: {signed_app}"
        )
    signed_binary = signed_app / f"{SCHEME}.debug.dylib"
    _run_quiet(
        [
            sys.executable,
            "-m",
            "tools.react_native.check_single_image",
            "ios",
            str(signed_binary),
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "signed-device-single-image-check.log",
        timeout=120,
    )
    xcrun = _require_executable("xcrun")
    _run_quiet(
        [
            xcrun,
            "devicectl",
            "device",
            "install",
            "app",
            "--device",
            udid,
            "--json-output",
            str(layout.log_root / "device-install.json"),
            str(signed_app),
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "device-install.log",
        timeout=180,
    )
    if ui_test:
        # XCUITest launches the app itself and exercises the same Nitro contract
        # through the visible RN UI. Avoid a preceding launch/terminate cycle:
        # it adds no evidence to this gate and makes device mirroring appear blank
        # while the runtime controller has already closed the app.
        _run_quiet(
            ui_test_command(layout, udid, team),
            cwd=layout.host_root / "ios",
            log=layout.log_root / "ui-test.log",
            timeout=900,
        )
        return

    runtime_log = layout.log_root / "device-runtime.log"
    _run_quiet(
        [
            sys.executable,
            "-m",
            "tools.react_native.run_ios_device_runtime",
            udid,
            str(runtime_log),
            "--seconds",
            "20",
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "device-runtime-controller.log",
        timeout=90,
    )
    _run_quiet(
        [
            sys.executable,
            "-m",
            "tools.react_native.check_runtime_log",
            str(runtime_log),
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "device-runtime-check.log",
        timeout=120,
    )


def run(
    *,
    work_root: Path = WORK_ROOT,
    device: tuple[str, str] | None = None,
    ui_test: bool = False,
) -> Layout:
    if sys.platform != "darwin":
        raise ArtifactConsumerError("iOS artifact consumer requires macOS/Xcode")
    layout = make_layout(_new_run_root(work_root))
    _prepare_work_tree(layout)
    print(f"react_native_ios_artifact_consumer_work={layout.root}", flush=True)
    stage_package(layout)
    tarball = pack_package(layout)
    verify_tarball(
        tarball,
        source_xcframework=APPLE_STAGED_ROOT
        / "BleHubUniFfi/BleHubUniFfiNative.xcframework",
        staged_xcframework=layout.package_root
        / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework",
    )
    create_host(layout, tarball)
    install_dependencies(layout)
    pod_install(layout)
    if ui_test and device is None:
        raise ArtifactConsumerError("UI test requires an explicit device")
    build_and_check(layout, device=device, ui_test=ui_test)
    scope = (
        "device-ui-test"
        if ui_test
        else "device-runtime"
        if device is not None
        else "static"
    )
    print(f"react_native_ios_artifact_consumer={scope}-passed work={layout.root}")
    return layout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-root",
        type=Path,
        default=WORK_ROOT,
        help="ignored disposable work directory (default: .work/react-native-ios-artifact-consumer)",
    )
    parser.add_argument(
        "--device-udid", help="explicit physical iPhone UDID for signed runtime"
    )
    parser.add_argument(
        "--development-team", help="10-character Apple Development Team"
    )
    parser.add_argument(
        "--ui-test",
        action="store_true",
        help="run the clean-artifact physical-device XCUITest after runtime smoke",
    )
    args = parser.parse_args(argv)
    if (args.device_udid is None) != (args.development_team is None):
        parser.error("--device-udid and --development-team must be supplied together")
    try:
        device = (
            (args.device_udid, args.development_team)
            if args.device_udid is not None and args.development_team is not None
            else None
        )
        run(work_root=args.work_root, device=device, ui_test=args.ui_test)
    except (ArtifactConsumerError, OSError, ValueError) as error:
        print(f"iOS artifact consumer error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
