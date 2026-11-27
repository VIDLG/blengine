"""Receipt support for the ONE canonical source/local-package iOS build owner.

No CLI, installer, subprocess implementation, or automatic adoption lives here.
The owner calls ``begin_preparation`` BEFORE an explicit refresh, runs its own
bounded ``pod install``, then calls ``seal_preparation(refresh_succeeded=True)``
only after success. Ordinary builds call ``verify_preparation``; an existing
unreceipted installation is never adopted. This is local cache evidence, not a
release gate, security attestation, or universal CocoaPods dependency analysis.

All snapshot functions take keyword-only ``ios_root``, ``host_root``,
``staging_root`` (Path), ``run``, and optional ``environment`` (the effective
Pod-affecting environment, default os.environ). ``run(command, *, cwd, timeout)``
returns stdout as str, raising on nonzero exit/timeout. It receives a command
starting with ``ruby``: the parent MUST select the same Ruby/gem installation as
its pod executable, close stdin, bound stdout/stderr (MAX_SNAPSHOT_BYTES), and
terminate/reap the entire owned process group on timeout. No fallback/install is
allowed. The Ruby helper only reads installed JSON; it never evaluates a Podfile
or a Ruby Podspec. Snapshot timeout is 120 seconds per callback.

Environment fingerprints contain hashes, never values. The parent must pass the
same effective environment for before/after/verify, including supported RN/Pod
flags and repository overrides. Transient PATH/CURL_HOME/proxy/curl transport
controls are deliberately excluded: the parent owns their scope and the fixed
local executables; this receipt is not proof of network-zero process execution.
The parent must serialize preparation/builds against other writers. A failed
refresh leaves no usable receipt. A caller falsely claiming successful explicit
refresh is outside this local, in-process ownership contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = 1
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_JSON_NODES = 500_000
SNAPSHOT_TIMEOUT = 120
RUBY_HELPER = Path(__file__).with_name("ios_pod_graph.rb")
ENVIRONMENT_PREFIXES = (
    "RCT_",
    "REACT_NATIVE_",
    "RN_",
    "HERMES_",
    "USE_",
    "COCOAPODS_",
    "CODEGEN_",
    "CP_",
    "POD_",
    "PODS_",
    "BUNDLE_",
    "BLEHUB_APPLE_",
    "npm_config_",
)
ENVIRONMENT_NAMES = frozenset(
    {
        "ENTERPRISE_REPOSITORY",
        "NODE_BINARY",
        "NODE_ENV",
        "NO_FLIPPER",
        "NODE_OPTIONS",
        "RUBYOPT",
        "RUBYLIB",
        "GEM_HOME",
        "GEM_PATH",
        "DEVELOPER_DIR",
        "SDKROOT",
        "ARCHS",
        "EXCLUDED_ARCHS",
        "IPHONEOS_DEPLOYMENT_TARGET",
        "SWIFT_VERSION",
        "CC",
        "CXX",
        "CFLAGS",
        "CXXFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "CI",
        "DISABLE_CODEGEN",
        "PRODUCTION",
        "FRAMEWORK_SEARCH_PATHS",
        "HEADER_SEARCH_PATHS",
    }
)
Run = Callable[..., str]


class PodPreparationError(RuntimeError):
    """Missing, stale, unsupported, or unsuccessful local Pods preparation."""


def _json_bytes(value: object) -> bytes:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise PodPreparationError("invalid preparation JSON") from error
    return raw.encode("utf-8")


def _validate_json(value: object) -> None:
    pending = [(value, 0)]
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > 64:
            raise PodPreparationError("preparation JSON structure limit exceeded")
        if isinstance(current, dict):
            if any(type(key) is not str for key in current):
                raise PodPreparationError("preparation JSON keys must be strings")
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif current is not None and type(current) not in (str, int, bool):
            raise PodPreparationError("unsupported preparation JSON value")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PodPreparationError("duplicate preparation JSON key")
        result[key] = value
    return result


def _parse(raw: str, maximum: int) -> dict[str, Any]:
    if type(raw) is not str or len(raw.encode("utf-8")) > maximum:
        raise PodPreparationError("preparation JSON output limit exceeded")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as error:
        raise PodPreparationError("invalid preparation JSON output") from error
    _validate_json(value)
    if not isinstance(value, dict):
        raise PodPreparationError("preparation JSON must be an object")
    return value


def _roots(ios_root: Path, host_root: Path, staging_root: Path) -> dict[str, str]:
    try:
        roots = {
            name: str(path.resolve(strict=True))
            for name, path in (
                ("ios", ios_root),
                ("host", host_root),
                ("staging", staging_root),
            )
        }
        if any(not Path(path).is_dir() for path in roots.values()):
            raise PodPreparationError("preparation roots must be existing directories")
        Path(roots["ios"]).relative_to(roots["host"])
    except (OSError, ValueError, RuntimeError) as error:
        raise PodPreparationError("invalid or missing preparation roots") from error
    return roots


def _environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    effective = os.environ if environment is None else environment
    return {
        key: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for key, value in sorted(effective.items())
        if key in ENVIRONMENT_NAMES or key.startswith(ENVIRONMENT_PREFIXES)
    }


def _implementation() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), RUBY_HELPER)
    }


def _snapshot(kind: str, roots: dict[str, str], run: Run) -> dict[str, Any]:
    command = [
        "ruby",
        str(RUBY_HELPER),
        kind,
        roots["ios"],
        roots["host"],
        roots["staging"],
    ]
    try:
        raw = run(command, cwd=Path(roots["ios"]), timeout=SNAPSHOT_TIMEOUT)
    except Exception as error:
        raise PodPreparationError(
            f"Pods {kind} snapshot failed; explicit refresh may be required"
        ) from error
    value = _parse(raw, MAX_SNAPSHOT_BYTES)
    if (
        set(value) != {"schema", "kind", "snapshot"}
        or type(value["schema"]) is not int
        or value["schema"] != SCHEMA
        or value["kind"] != kind
        or not isinstance(value["snapshot"], dict)
        or value["snapshot"].get("roots") != roots
    ):
        raise PodPreparationError("unexpected Pods snapshot schema/kind/roots")
    snapshot = value["snapshot"]
    required = (
        {"roots", "packages", "metadata"}
        if kind == "inputs"
        else {
            "roots",
            "toolchain",
            "lock",
            "manifest",
            "podfile",
            "specifications",
            "targets",
            "integration",
        }
    )
    if set(snapshot) != required:
        raise PodPreparationError("incomplete Pods snapshot")
    if kind == "inputs":
        if (
            not isinstance(snapshot["packages"], list)
            or not snapshot["packages"]
            or not isinstance(snapshot["metadata"], dict)
            or not snapshot["metadata"]
        ):
            raise PodPreparationError("empty declared Pods inputs")
    elif (
        not isinstance(snapshot["specifications"], dict)
        or not snapshot["specifications"]
        or not isinstance(snapshot["targets"], list)
        or not snapshot["targets"]
        or not isinstance(snapshot["integration"], dict)
        or not snapshot["integration"]
    ):
        raise PodPreparationError("empty installed Pods graph")
    return snapshot


def preparation_inputs(
    *,
    ios_root: Path,
    host_root: Path,
    staging_root: Path,
    run: Run,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Capture declared metadata before refresh; Pods/lock may not exist yet.

    Generated ios/build Pods/codegen definitions are excluded here. They belong
    to the installed snapshot taken after a successful explicit installation.
    Existing npm dependencies and staging inputs MUST already be prepared.
    """
    roots = _roots(ios_root, host_root, staging_root)
    return {
        "schema": SCHEMA,
        "roots": roots,
        "environment": _environment(environment),
        "implementation": _implementation(),
        "declared": _snapshot("inputs", roots, run),
    }


def _receipt_path(receipt: Path) -> Path:
    path = receipt.absolute()
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise PodPreparationError("receipt path must not contain symlinks")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise PodPreparationError("receipt must be a regular file")
    return path


def begin_preparation(receipt: Path, **kwargs: Any) -> dict[str, Any]:
    """Invalidate old evidence BEFORE explicit installation, then capture inputs.

    The owner calls this only in its --refresh-pods branch, not ordinary builds.
    On any later failure do not seal; verification will require a fresh refresh.
    """
    path = _receipt_path(receipt)
    path.unlink(missing_ok=True)
    return preparation_inputs(**kwargs)


def _difference(
    expected: object, actual: object, prefix: str = "snapshot"
) -> str | None:
    if type(expected) is not type(actual):
        return prefix
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys()):
            location = f"{prefix}.{key}"
            if key not in expected or key not in actual:
                return location
            result = _difference(expected[key], actual[key], location)
            if result:
                return result
        return None
    return None if expected == actual else prefix


def _require_equal(expected: object, actual: object, label: str) -> None:
    difference = _difference(expected, actual, label)
    if difference:
        # Report a field/path, never environment or secret-bearing file contents.
        raise PodPreparationError(
            f"stale Pods preparation: {difference}; run explicit --refresh-pods"
        )


def seal_preparation(
    receipt: Path,
    *,
    before: dict[str, Any],
    refresh_succeeded: bool,
    ios_root: Path,
    host_root: Path,
    staging_root: Path,
    run: Run,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Seal ONLY after the parent's explicit install succeeds, returning receipt.

    Declared inputs must not change across install. Derived installed outputs
    may legitimately be created/replaced. The owner must have called begin first;
    an existing receipt is refused rather than silently replaced/adopted.
    """
    path = _receipt_path(receipt)
    if refresh_succeeded is not True:
        raise PodPreparationError("cannot seal without successful explicit refresh")
    if path.exists():
        raise PodPreparationError(
            "begin_preparation must invalidate the previous receipt first"
        )
    kwargs = {
        "ios_root": ios_root,
        "host_root": host_root,
        "staging_root": staging_root,
        "run": run,
        "environment": environment,
    }
    after = preparation_inputs(**kwargs)
    _require_equal(before, after, "declared inputs changed during refresh")
    installed = _snapshot("installed", after["roots"], run)
    _require_equal(
        after, preparation_inputs(**kwargs), "inputs changed during installed snapshot"
    )
    result = {
        "schema": SCHEMA,
        "scope": "source-local-installed-pods",
        "inputs": after,
        "installed": installed,
    }
    raw = _json_bytes(result)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise PodPreparationError("preparation receipt limit exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _receipt_path(path)
        # Exclusive publication: another writer is not permission to replace a
        # receipt. The owner serializes installs; a race still fails closed.
        os.link(temporary, path)
    except OSError as error:
        raise PodPreparationError(
            "could not publish Pods preparation receipt"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result


def verify_preparation(
    receipt: Path,
    *,
    ios_root: Path,
    host_root: Path,
    staging_root: Path,
    run: Run,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read-only admission: require sealed evidence and compare fresh snapshots.

    Never creates, updates or adopts a receipt; ordinary source-byte edits do
    not invalidate membership. Metadata/project/support/environment changes do.
    """
    path = _receipt_path(receipt)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            raise PodPreparationError("preparation receipt limit exceeded")
        saved = _parse(raw.decode("utf-8"), MAX_RECEIPT_BYTES)
    except (OSError, UnicodeError) as error:
        raise PodPreparationError(
            "missing/unreadable Pods preparation receipt; run explicit --refresh-pods"
        ) from error
    if (
        set(saved) != {"schema", "scope", "inputs", "installed"}
        or type(saved["schema"]) is not int
        or saved["schema"] != SCHEMA
        or saved["scope"] != "source-local-installed-pods"
        or not isinstance(saved["inputs"], dict)
        or not isinstance(saved["installed"], dict)
    ):
        raise PodPreparationError(
            "invalid Pods preparation receipt; run explicit --refresh-pods"
        )
    kwargs = {
        "ios_root": ios_root,
        "host_root": host_root,
        "staging_root": staging_root,
        "run": run,
        "environment": environment,
    }
    current = preparation_inputs(**kwargs)
    _require_equal(saved["inputs"], current, "inputs")
    installed = _snapshot("installed", current["roots"], run)
    _require_equal(saved["installed"], installed, "installed")
    _require_equal(
        current, preparation_inputs(**kwargs), "inputs changed during verification"
    )
    return saved
