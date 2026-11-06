"""Build a clean, artifact-only React Native Android consumer.

The consumer installs the private integration from an npm tarball, resolves
its Android module through RN autolinking, and builds only against published
fixture Maven artifacts. It never uses the integration checkout as a Gradle
project dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from tools.react_native.run_android_runtime import run as run_android_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = PROJECT_ROOT / "integrations/react-native-internal"
HOST_SOURCE_ROOT = PROJECT_ROOT / "tests/consumers/react-native-internal"
ANDROID_MAVEN_ROOT = PROJECT_ROOT / ".work/staged/android-maven"
WORK_ROOT = PROJECT_ROOT / ".work/react-native-android-artifact-consumer"
PACKAGE_NAME = "@blehub/react-native-internal"
TAIL_LINES = 80
JsonObject = dict[str, object]

INTEGRATION_FILES: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "react-native.config.js",
    "src",
    "specs",
    "nitrogen/generated/android",
    "nitrogen/generated/shared",
    "android/CMakeLists.txt",
    "android/build.gradle",
    "android/gradle.properties",
    "android/src/main",
)
HOST_FILES: tuple[str, ...] = (
    ".eslintrc.js",
    ".prettierrc.js",
    ".watchmanconfig",
    "app.json",
    "babel.config.js",
    "index.js",
    "metro.config.js",
    "package-lock.json",
    "package.json",
    "src",
    "android/app",
    "android/build.gradle",
    "android/gradle",
    "android/gradle.properties",
    "android/gradlew",
    "android/gradlew.bat",
)
FORBIDDEN_DIR_NAMES = frozenset({".cxx", "Pods", "build", "node_modules"})
TEXTUAL_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".gradle",
        ".h",
        ".hpp",
        ".js",
        ".json",
        ".kt",
        ".lock",
        ".md",
        ".properties",
        ".toml",
        ".ts",
        ".tsx",
    }
)
TEXTUAL_NAMES = frozenset({"CMakeLists.txt", "gradlew"})

RN_CONFIG = """const path = require('node:path');

const integrationRoot = path.dirname(
  require.resolve('@blehub/react-native-internal/package.json'),
);

module.exports = {
  dependencies: {
    '@blehub/react-native-internal': {
      root: integrationRoot,
      platforms: {
        android: { sourceDir: path.join(integrationRoot, 'android') },
        ios: {},
      },
    },
  },
};
"""

METRO_CONFIG = """const path = require('node:path');
const { getDefaultConfig, mergeConfig } = require('@react-native/metro-config');

const integrationRoot = path.dirname(
  require.resolve('@blehub/react-native-internal/package.json'),
);
const hostNodeModules = path.resolve(__dirname, 'node_modules');

module.exports = mergeConfig(getDefaultConfig(__dirname), {
  watchFolders: [integrationRoot],
  resolver: {
    blockList: [new RegExp(`${integrationRoot}/node_modules/.*`)],
    extraNodeModules: {
      '@blehub/react-native-internal': integrationRoot,
      react: path.join(hostNodeModules, 'react'),
      'react-native': path.join(hostNodeModules, 'react-native'),
      'react-native-nitro-modules': path.join(hostNodeModules, 'react-native-nitro-modules'),
    },
  },
});
"""

ANDROID_SETTINGS = """pluginManagement {
  includeBuild('../node_modules/@react-native/gradle-plugin')
  repositories {
    maven { url = uri('https://mirrors.huaweicloud.com/repository/maven/') }
    gradlePluginPortal()
  }
}

plugins { id('com.facebook.react.settings') }
extensions.configure(com.facebook.react.ReactSettingsExtension) { ex ->
  ex.autolinkLibrariesFromCommand()
}

dependencyResolutionManagement {
  repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
  repositories {
    maven { url = uri(rootDir.toPath().resolve('../../artifacts/android-maven')) }
    maven { url = uri('https://mirrors.huaweicloud.com/repository/maven/') }
  }
}

rootProject.name = 'BleHubNitroHost'
include ':app'
includeBuild('../node_modules/@react-native/gradle-plugin')
"""


class ArtifactConsumerError(RuntimeError):
    """Raised when the clean Android artifact boundary cannot be proven."""


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
    def apk(self) -> Path:
        return self.host_root / "android/app/build/outputs/apk/debug/app-debug.apk"

    @property
    def release_apk(self) -> Path:
        return self.host_root / "android/app/build/outputs/apk/release/app-release.apk"


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
        _ = path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise ArtifactConsumerError(f"missing required executable: {name}")
    return executable


def _copy_checked(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ArtifactConsumerError(f"refusing symlinked source entry: {source}")
    if not source.exists():
        raise ArtifactConsumerError(f"missing whitelisted source entry: {source}")
    if source.is_dir():
        for child in source.rglob("*"):
            if child.is_symlink() and not any(
                part in FORBIDDEN_DIR_NAMES for part in child.relative_to(source).parts
            ):
                raise ArtifactConsumerError(f"refusing symlink in source tree: {child}")
        _ = shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns(*FORBIDDEN_DIR_NAMES),
        )
    elif source.is_file():
        _ = destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(source, destination)
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


def _assert_no_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArtifactConsumerError(f"symlink in disposable tree: {path}")


def _prepare_work_tree(layout: Layout) -> None:
    root = layout.root.resolve()
    project = PROJECT_ROOT.resolve()
    if not _is_relative_to(root, project / ".work") or root == project / ".work":
        raise ArtifactConsumerError(
            f"work tree must be a fresh child of {project / '.work'}"
        )
    if layout.root.exists() or layout.root.is_symlink():
        raise ArtifactConsumerError(
            f"refusing to reuse artifact run directory: {layout.root}"
        )
    _ = layout.root.mkdir(parents=True, exist_ok=False)
    layout.package_root.mkdir()
    layout.artifact_root.mkdir()
    layout.log_root.mkdir()


def _new_run_root(parent: Path = WORK_ROOT) -> Path:
    return parent / f"run-{uuid.uuid4().hex}"


def stage_package(layout: Layout) -> None:
    _copy_whitelist(INTEGRATION_ROOT, layout.package_root, INTEGRATION_FILES)
    required = (
        layout.package_root / "package.json",
        layout.package_root / "android/build.gradle",
        layout.package_root / "android/CMakeLists.txt",
        layout.package_root / "android/src/main",
        layout.package_root / "nitrogen/generated/android/kotlin",
        layout.package_root / "nitrogen/generated/shared/c++",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ArtifactConsumerError(f"staged Android package is incomplete: {missing}")
    _assert_no_symlinks(layout.package_root)
    _assert_no_checkout_leakage(layout.package_root)


def _forbidden_tokens() -> tuple[bytes, ...]:
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


def _is_textual(path: Path) -> bool:
    return path.name in TEXTUAL_NAMES or path.suffix in TEXTUAL_SUFFIXES


def _assert_no_checkout_leakage(root: Path) -> None:
    tokens = _forbidden_tokens()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ArtifactConsumerError(f"symlink escaped into clean tree: {path}")
        if not path.is_file() or not _is_textual(path):
            continue
        data = path.read_bytes()
        if any(token in data for token in tokens):
            raise ArtifactConsumerError(f"checkout path leakage in {path}")


def _run_quiet(
    command: Sequence[str], *, cwd: Path, log: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"[rn-android-artifact] running {Path(command[0]).name}; full log: {log}",
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
        _ = log.write_text(output, encoding="utf-8")
        raise ArtifactConsumerError(
            f"command timed out: {command[0]} (full log: {log})"
        ) from error
    _ = log.write_text(output, encoding="utf-8")
    if result.returncode != 0:
        tail = "\n".join(output.splitlines()[-TAIL_LINES:])
        raise ArtifactConsumerError(
            f"command failed with exit {result.returncode}: {command[0]}\n{tail}\nfull log: {log}"
        )
    return result


def pack_package(layout: Layout) -> Path:
    result = _run_quiet(
        [
            _require_executable("npm"),
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
        decoded: object = cast(object, json.loads(result.stdout.strip()))
        if isinstance(decoded, list):
            items = cast(list[object], decoded)
            metadata: object = items[0] if items else None
        else:
            metadata = decoded
        if not isinstance(metadata, dict):
            raise TypeError("npm metadata is not an object")
        metadata_object = cast(JsonObject, metadata)
        filename = metadata_object.get("filename")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ArtifactConsumerError(
            "npm pack did not return a usable tarball filename"
        ) from error
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ArtifactConsumerError(
            f"npm returned unsafe tarball filename: {filename!r}"
        )
    tarball = layout.artifact_root / filename
    if not tarball.is_file() or tarball.is_symlink():
        raise ArtifactConsumerError(f"npm pack did not create {tarball}")
    return tarball


def verify_tarball(tarball: Path) -> None:
    required_prefixes = (
        "package/android/src/main/",
        "package/nitrogen/generated/android/",
        "package/nitrogen/generated/shared/",
    )
    names: list[str] = []
    tokens = _forbidden_tokens()
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            seen: set[str] = set()
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
                names.append(member.name)
                if not member.isfile() and not member.isdir():
                    raise ArtifactConsumerError(
                        f"unsupported npm tarball entry: {member.name}"
                    )
                if member.isfile() and _is_textual(Path(member.name)):
                    stream = archive.extractfile(member)
                    if stream is not None:
                        data = stream.read()
                        if any(token in data for token in tokens):
                            raise ArtifactConsumerError(
                                f"checkout path leakage in npm entry: {member.name}"
                            )
    except (OSError, tarfile.TarError) as error:
        raise ArtifactConsumerError(
            f"unable to inspect npm tarball {tarball}: {error}"
        ) from error
    for prefix in required_prefixes:
        if not any(name.startswith(prefix) for name in names):
            raise ArtifactConsumerError(
                f"npm tarball is missing required Android surface: {prefix}"
            )
    if (
        "package/package.json" not in names
        or "package/android/build.gradle" not in names
    ):
        raise ArtifactConsumerError(
            "npm tarball is missing package.json or Android build.gradle"
        )


def _read_json(path: Path) -> JsonObject:
    try:
        value: object = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactConsumerError(f"invalid JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise ArtifactConsumerError(f"JSON root is not an object: {path}")
    return cast(JsonObject, value)


def _write_json(path: Path, value: JsonObject) -> None:
    _ = path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _rewrite_host_package(host_root: Path, tarball: Path) -> None:
    package_path = host_root / "package.json"
    package = _read_json(package_path)
    dependencies = package.get("dependencies")
    if not isinstance(dependencies, dict) or PACKAGE_NAME not in dependencies:
        raise ArtifactConsumerError(f"host package has no {PACKAGE_NAME} dependency")
    dependencies[PACKAGE_NAME] = (
        f"file:{os.path.relpath(tarball, host_root).replace(os.sep, '/')}"
    )
    _write_json(package_path, package)


def _rewrite_host_lockfile(host_root: Path, tarball: Path) -> None:
    lock_path = host_root / "package-lock.json"
    lock = _read_json(lock_path)
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ArtifactConsumerError("host package-lock.json has no packages object")
    packages_object = cast(JsonObject, packages)
    root = packages_object.get("")
    if not isinstance(root, dict):
        raise ArtifactConsumerError("host package-lock.json has no root package")
    root_object = cast(JsonObject, root)
    if not isinstance(root_object.get("dependencies"), dict):
        raise ArtifactConsumerError("host package-lock.json has no root dependencies")
    relative = f"file:{os.path.relpath(tarball, host_root).replace(os.sep, '/')}"
    dependencies = root_object["dependencies"]
    assert isinstance(dependencies, dict)
    dependencies_object = cast(JsonObject, dependencies)
    dependencies_object[PACKAGE_NAME] = relative
    for key in list(packages_object):
        if (
            "integrations/react-native-internal" in key
            or key == f"node_modules/{PACKAGE_NAME}"
        ):
            del packages_object[key]
    _write_json(lock_path, lock)


def create_host(layout: Layout, tarball: Path) -> None:
    _copy_whitelist(HOST_SOURCE_ROOT, layout.host_root, HOST_FILES)
    _rewrite_host_package(layout.host_root, tarball)
    _rewrite_host_lockfile(layout.host_root, tarball)
    _ = (layout.host_root / "react-native.config.js").write_text(
        RN_CONFIG, encoding="utf-8"
    )
    _ = (layout.host_root / "metro.config.js").write_text(
        METRO_CONFIG, encoding="utf-8"
    )
    _ = (layout.host_root / "android/settings.gradle").write_text(
        ANDROID_SETTINGS, encoding="utf-8"
    )
    _assert_no_symlinks(layout.host_root)
    _assert_no_checkout_leakage(layout.host_root)


def stage_maven(layout: Layout) -> None:
    if not ANDROID_MAVEN_ROOT.is_dir() or ANDROID_MAVEN_ROOT.is_symlink():
        raise ArtifactConsumerError(
            f"missing staged Android Maven repository: {ANDROID_MAVEN_ROOT}; run the production Android publish recipe first"
        )
    destination = layout.artifact_root / "android-maven"
    _copy_checked(ANDROID_MAVEN_ROOT, destination)
    _assert_no_symlinks(destination)


def install_dependencies(layout: Layout) -> None:
    npm = _require_executable("npm")
    _ = _run_quiet(
        [npm, "install", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=layout.host_root,
        log=layout.log_root / "npm-install.log",
        timeout=600,
    )
    installed = layout.host_root / "node_modules/@blehub/react-native-internal"
    if (
        not installed.is_dir()
        or installed.is_symlink()
        or not _is_relative_to(installed.resolve(), layout.host_root.resolve())
    ):
        raise ArtifactConsumerError(
            f"RN package did not install inside clean host: {installed}"
        )
    _assert_no_checkout_leakage(installed)
    rn = layout.host_root / "node_modules/.bin/react-native"
    result = _run_quiet(
        [str(rn), "config", "--platform", "android"],
        cwd=layout.host_root,
        log=layout.log_root / "rn-android-config.log",
        timeout=120,
    )
    try:
        config: object = cast(object, json.loads(result.stdout))
        if not isinstance(config, dict):
            raise TypeError("RN config is not an object")
        config_object = cast(JsonObject, config)
        dependencies = config_object.get("dependencies")
        if not isinstance(dependencies, dict):
            raise TypeError("RN config has no dependencies")
        dependencies_object = cast(JsonObject, dependencies)
        dependency = dependencies_object.get(PACKAGE_NAME)
        if not isinstance(dependency, dict):
            raise TypeError("RN config has no installed BleHub dependency")
        dependency_object = cast(JsonObject, dependency)
        root = Path(cast(str, dependency_object["root"]))
        platforms = dependency_object.get("platforms")
        if not isinstance(platforms, dict):
            raise TypeError("RN config has no platforms")
        platforms_object = cast(JsonObject, platforms)
        android = platforms_object.get("android")
        if not isinstance(android, dict):
            raise TypeError("RN config has no Android platform")
        android_object = cast(JsonObject, android)
        source_dir = Path(cast(str, android_object["sourceDir"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactConsumerError(
            "RN CLI did not resolve the installed Android module"
        ) from error
    if root != installed or source_dir != installed / "android":
        raise ArtifactConsumerError(
            f"RN CLI resolved outside installed artifact: root={root}, sourceDir={source_dir}"
        )


def build_and_check(layout: Layout) -> None:
    gradlew = layout.host_root / "android/gradlew"
    if not gradlew.is_file():
        raise ArtifactConsumerError(f"clean host is missing Gradle wrapper: {gradlew}")
    _ = _run_quiet(
        [
            _require_executable("pixi"),
            "run",
            "-e",
            "android",
            str(gradlew),
            "-p",
            str(layout.host_root / "android"),
            ":app:assembleDebug",
            "--no-daemon",
        ],
        cwd=layout.host_root / "android",
        log=layout.log_root / "gradle-build.log",
        timeout=1200,
    )
    if not layout.apk.is_file():
        raise ArtifactConsumerError(
            f"clean artifact host did not produce APK: {layout.apk}"
        )
    _ = _run_quiet(
        [
            sys.executable,
            "-m",
            "tools.react_native.check_single_image",
            "android",
            str(layout.apk),
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "single-image-check.log",
        timeout=120,
    )


def build_release_for_device(layout: Layout) -> None:
    """Build the clean artifact host with its bundled JS for a physical device."""
    gradlew = layout.host_root / "android/gradlew"
    _ = _run_quiet(
        [
            _require_executable("pixi"),
            "run",
            "-e",
            "android",
            str(gradlew),
            "-p",
            str(layout.host_root / "android"),
            ":app:assembleRelease",
            "--no-daemon",
        ],
        cwd=layout.host_root / "android",
        log=layout.log_root / "gradle-release-build.log",
        timeout=1200,
    )
    if not layout.release_apk.is_file():
        raise ArtifactConsumerError(
            f"clean artifact host did not produce release APK: {layout.release_apk}"
        )
    _ = _run_quiet(
        [
            sys.executable,
            "-m",
            "tools.react_native.check_single_image",
            "android",
            str(layout.release_apk),
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "release-single-image-check.log",
        timeout=120,
    )


def run_device_lifecycle(layout: Layout, serial: str) -> None:
    """Install the clean artifact release APK and run the shared lifecycle gate."""
    if not serial:
        raise ArtifactConsumerError("Android device serial must not be empty")
    build_release_for_device(layout)
    _ = _run_quiet(
        [
            _require_executable("pixi"),
            "run",
            "-e",
            "android",
            "python",
            "-m",
            "tools.android.install_apk",
            "--serial",
            serial,
            "--device",
            "physical",
            "--apk",
            str(layout.release_apk),
            "--log",
            str(layout.log_root / "apk-install.log"),
        ],
        cwd=PROJECT_ROOT,
        log=layout.log_root / "apk-install-command.log",
        timeout=180,
    )
    _ = run_android_runtime(
        serial,
        "stress",
        layout.log_root / "lifecycle-stress.log",
        timeout_seconds=90,
    )


def run(*, work_root: Path = WORK_ROOT, device_serial: str | None = None) -> Layout:
    layout = make_layout(_new_run_root(work_root))
    _prepare_work_tree(layout)
    print(f"react_native_android_artifact_consumer_work={layout.root}", flush=True)
    stage_package(layout)
    tarball = pack_package(layout)
    verify_tarball(tarball)
    stage_maven(layout)
    create_host(layout, tarball)
    install_dependencies(layout)
    build_and_check(layout)
    if device_serial is not None:
        run_device_lifecycle(layout, device_serial)
        print(
            f"react_native_android_artifact_consumer=device-lifecycle-passed device={device_serial} work={layout.root}",
            flush=True,
        )
    else:
        print(
            f"react_native_android_artifact_consumer=static-passed work={layout.root}",
            flush=True,
        )
    return layout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--work-root", type=Path, default=WORK_ROOT)
    _ = parser.add_argument("--device")
    args = parser.parse_args(argv)
    try:
        _ = run(
            work_root=cast(Path, args.work_root),
            device_serial=cast(str | None, args.device),
        )
    except (ArtifactConsumerError, OSError, ValueError) as error:
        print(f"Android artifact consumer error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
