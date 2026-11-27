"""Report or remove bounded, reproducible BleHub working-tree artifacts.

`.work` is local scratch, and several of its roots grow without bound: every consumer
run copies its own dependency tree and build output. This tool states that policy in
one table (`POLICY`) instead of one `if` per root, so a new generated root is a row and
a reader can see, in one place, what is deleted by default and what costs a rebuild.

Two retention classes exist:

* `run-window` — a directory of `run-*` children; the newest ``keep_runs`` survive.
* `whole` — the root is deleted as one unit.

Anything not in the table is never touched, and no path outside the declared roots can
be removed even if a row were wrong.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Retention classes.
RUN_WINDOW = "run-window"
WHOLE = "whole"

# A run in an evidence root is named by its own UUID, not by a `run-` prefix.
_RUN_DIRECTORY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")


@dataclass(frozen=True)
class GeneratedRoot:
    """One generated root and the policy that bounds it.

    A `path` whose last component contains `*` declares a *family*: every sibling directory
    matching it is its own run window. One row then covers every scene of a lane, so adding a
    scene cannot silently create an unbounded root.
    """

    label: str
    path: Path
    reason: str
    retention: str
    # An opt-in root is reproducible but expensive: deleting it makes the next build
    # or the next device run substantially slower, so it needs an explicit flag.
    opt_in: str | None = None
    # What counts as one retained unit inside a run window. Consumers name runs `run-*`; an
    # evidence root names a run by its own UUID, so its window matches the UUID shape rather
    # than "any child" — infrastructure children (installed toolchains, staged package
    # images) then stay out of a window even if a row's path were widened.
    child_prefix: str = "run-"


def _declared_roots(project_root: Path) -> list[tuple[GeneratedRoot, Path]]:
    """Expand every policy row to the concrete directories it owns, right now."""
    declared: list[tuple[GeneratedRoot, Path]] = []
    for root in POLICY:
        if "*" in root.path.name:
            parent = project_root / root.path.parent
            if not parent.is_dir():
                continue
            for child in sorted(parent.glob(root.path.name)):
                if child.is_dir() and not child.is_symlink():
                    declared.append((root, child))
        else:
            declared.append((root, project_root / root.path))
    return declared


# Ordered as a reader would scan it: run windows first, then host output, then caches.
POLICY: tuple[GeneratedRoot, ...] = (
    GeneratedRoot(
        "rn-artifact-run",
        Path(".work/react-native-ios-artifact-consumer"),
        "consumer run copies older than the retained window",
        RUN_WINDOW,
    ),
    GeneratedRoot(
        "rn-artifact-run",
        Path(".work/react-native-android-artifact-consumer"),
        "consumer run copies older than the retained window",
        RUN_WINDOW,
    ),
    # Evidence lanes: a run is a UUID directory, so one family row per container would be a list
    # to keep growing. `*` expands to every direct `.work` child and each becomes its own window;
    # the UUID run unit is what keeps infrastructure children (installed toolchains, staged
    # package images, lease records) out of every window, so no per-lane row is needed.
    GeneratedRoot(
        "rn-evidence-run",
        Path(".work/*"),
        "run attempts older than the retained window",
        RUN_WINDOW,
        child_prefix="",
    ),
    # An unreferenced Gradle tree left by an earlier consumer layout. Nothing reads it.
    GeneratedRoot(
        "gradle-scratch",
        Path(".work/react-native-bleng-android"),
        "unreferenced Gradle scratch tree",
        WHOLE,
    ),
    # The formal host is the live lane. Its output is reproducible, but rebuilding it is
    # minutes of Gradle and Xcode work, so it is opt-in and keeps the installed candidate
    # APK reachable at the path the run command names.
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/node_modules"),
        "formal RN host dependencies",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/ios/Pods"),
        "formal host CocoaPods output",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/ios/build"),
        "formal host iOS build output",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/ios/BleHubBlengHost.xcworkspace"),
        "formal host generated CocoaPods workspace",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/ios/.xcode.env.local"),
        "formal host local Xcode environment",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/android/app/.cxx"),
        "formal host Android CMake output",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "rn-host-generated",
        Path("tests/consumers/react-native-bleng/android/app/build"),
        "formal host Android app build output",
        WHOLE,
        opt_in="formal-host-generated",
    ),
    GeneratedRoot(
        "archive-local",
        Path(".work/archive-local"),
        "local copies of already archived sources",
        WHOLE,
        opt_in="archive-local",
    ),
    GeneratedRoot(
        "cargo-target",
        Path(".work/cargo-target"),
        "rebuildable Rust target cache",
        WHOLE,
        opt_in="cargo-target",
    ),
)

# Every flag this tool understands for opting into an expensive root.
OPT_IN_FLAGS = tuple(
    dict.fromkeys(root.opt_in for root in POLICY if root.opt_in is not None)
)


@dataclass(frozen=True)
class CleanupItem:
    label: str
    path: Path
    reason: str


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_allowed(path: Path, allowed_roots: Iterable[Path]) -> None:
    resolved = path.resolve(strict=False)
    if not any(
        _is_relative_to(resolved, root.resolve(strict=False)) for root in allowed_roots
    ):
        raise RuntimeError(f"refusing cleanup outside generated roots: {path}")


def _disk_bytes(path: Path) -> int:
    if path.is_symlink():
        return path.lstat().st_size
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(root) / name).is_symlink()
        ]
        for name in files:
            entry = Path(root) / name
            try:
                if not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                pass
    return total


def _format_bytes(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or suffix == "GiB":
            return f"{size:.1f} {suffix}"
        size /= 1024
    return f"{value} B"


def build_plan(
    project_root: Path = PROJECT_ROOT,
    *,
    keep_runs: int = 1,
    include_archive_local: bool = False,
    include_cargo_target: bool = False,
    include_formal_host_generated: bool = False,
    protect_cited: bool = True,
) -> list[CleanupItem]:
    if keep_runs < 0:
        raise ValueError("keep_runs must be non-negative")

    included_opt_ins = {
        name
        for name, enabled in (
            ("archive-local", include_archive_local),
            ("cargo-target", include_cargo_target),
            ("formal-host-generated", include_formal_host_generated),
        )
        if enabled
    }
    # A run named by an authored document is evidence: reports cite run ids, so deleting one
    # would break a citation the tree still makes. This is a *protection*, not a retention
    # window: a cited run survives any `keep_runs`.
    citations = _citation_text(project_root) if protect_cited else ""
    roots = [path for _, path in _declared_roots(project_root)]
    items: list[CleanupItem] = []
    seen: set[Path] = set()
    for root, path in _declared_roots(project_root):
        if root.opt_in is not None and root.opt_in not in included_opt_ins:
            continue
        if root.retention == RUN_WINDOW:
            if not path.is_dir():
                continue
            runs = sorted(
                (
                    child
                    for child in path.iterdir()
                    if (child.is_dir() or child.is_symlink())
                    and (
                        child.name.startswith(root.child_prefix)
                        if root.child_prefix
                        else _RUN_DIRECTORY.match(child.name) is not None
                    )
                ),
                key=lambda child: child.stat().st_mtime,
                reverse=True,
            )
            for run in runs[keep_runs:]:
                if run.name in citations or run in seen:
                    continue
                _assert_allowed(run, roots)
                seen.add(run)
                items.append(CleanupItem(root.label, run, root.reason))
        elif path.exists() or path.is_symlink():
            _assert_allowed(path, roots)
            if path in seen:
                continue
            seen.add(path)
            items.append(CleanupItem(root.label, path, root.reason))
    return items


_CITATION_GLOBS = ("*.md", ".plan/*.md", "docs/**/*.md", "tests/consumers/*/README.md")


def _citation_text(project_root: Path = PROJECT_ROOT) -> str:
    """Every authored document that can cite a run id.

    Read-only and bounded: the tree's own prose, never `.work`, generated output or vendored
    dependency trees.
    """
    parts: list[str] = []
    for pattern in _CITATION_GLOBS:
        for path in sorted(project_root.glob(pattern)):
            if not path.is_file() or "/node_modules/" in path.as_posix():
                continue
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def apply_plan(items: Iterable[CleanupItem]) -> int:
    reclaimed = 0
    for item in items:
        item_bytes = _disk_bytes(item.path)
        reclaimed += item_bytes
        if item.path.is_symlink() or item.path.is_file():
            item.path.unlink()
        elif item.path.is_dir():
            shutil.rmtree(item.path)
        print(f"removed {item.label}: {item.path} ({_format_bytes(item_bytes)})")
    return reclaimed


def print_plan(items: Iterable[CleanupItem]) -> None:
    items = list(items)
    total = 0
    print(f"cleanup_items={len(items)}")
    for item in items:
        size = _disk_bytes(item.path)
        total += size
        print(
            f"would_remove={item.path} size={_format_bytes(size)} reason={item.reason}"
        )
    print(f"reclaimable={_format_bytes(total)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("report", "apply"))
    parser.add_argument("--keep-runs", type=int, default=1)
    parser.add_argument(
        "--include-archive-local",
        action="store_true",
        help="also remove local copies of already archived sources",
    )
    parser.add_argument(
        "--include-cargo-target",
        action="store_true",
        help="also remove the rebuildable Rust target cache",
    )
    parser.add_argument(
        "--include-formal-host-generated",
        action="store_true",
        help="also remove the live formal host's dependencies and build output",
    )
    args = parser.parse_args(argv)
    try:
        items = build_plan(
            keep_runs=args.keep_runs,
            include_archive_local=args.include_archive_local,
            include_cargo_target=args.include_cargo_target,
            include_formal_host_generated=args.include_formal_host_generated,
        )
        if args.action == "report":
            print_plan(items)
        else:
            reclaimed = apply_plan(items)
            print(f"reclaimed={_format_bytes(reclaimed)}")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"work cleanup error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
