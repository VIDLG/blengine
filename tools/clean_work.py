"""Report or remove bounded, reproducible BleHub working-tree artifacts.

The default policy removes only stale React Native consumer runs and generated
RN host output. Large shared caches and archived local copies require explicit
flags because deleting them is safe but makes the next build substantially
slower.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RN_ARTIFACT_ROOT = PROJECT_ROOT / ".work/react-native-ios-artifact-consumer"
RN_HOST_ROOT = PROJECT_ROOT / "tests/consumers/react-native-internal"
ARCHIVE_LOCAL_ROOT = PROJECT_ROOT / ".work/archive-local"
CARGO_TARGET_ROOT = PROJECT_ROOT / ".work/cargo-target"


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
) -> list[CleanupItem]:
    if keep_runs < 0:
        raise ValueError("keep_runs must be non-negative")

    rn_artifact_root = project_root / RN_ARTIFACT_ROOT.relative_to(PROJECT_ROOT)
    rn_host_root = project_root / RN_HOST_ROOT.relative_to(PROJECT_ROOT)
    archive_local_root = project_root / ARCHIVE_LOCAL_ROOT.relative_to(PROJECT_ROOT)
    cargo_target_root = project_root / CARGO_TARGET_ROOT.relative_to(PROJECT_ROOT)
    allowed_roots = (
        rn_artifact_root,
        rn_host_root,
        archive_local_root,
        cargo_target_root,
    )
    items: list[CleanupItem] = []

    if rn_artifact_root.is_dir():
        runs = sorted(
            (
                path
                for path in rn_artifact_root.iterdir()
                if path.name.startswith("run-")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in runs[keep_runs:]:
            _assert_allowed(path, allowed_roots)
            items.append(
                CleanupItem("rn-artifact-run", path, "older than retained run window")
            )

    legacy_artifact_paths = (
        (rn_artifact_root / "artifacts", "legacy artifact staging root"),
        (rn_artifact_root / "host", "legacy source-linked host root"),
        (rn_artifact_root / "logs", "legacy artifact consumer logs"),
        (rn_artifact_root / "package", "legacy package staging root"),
    )
    for path, reason in legacy_artifact_paths:
        if path.exists() or path.is_symlink():
            _assert_allowed(path, allowed_roots)
            items.append(CleanupItem("rn-artifact-legacy", path, reason))

    generated_host_paths = (
        (rn_host_root / "node_modules", "RN host dependencies"),
        (rn_host_root / "ios/Pods", "CocoaPods output"),
        (rn_host_root / "ios/build", "iOS DerivedData/build output"),
        (
            rn_host_root / "ios/BleHubNitroHost.xcworkspace",
            "generated CocoaPods workspace",
        ),
        (rn_host_root / "ios/.xcode.env.local", "generated local Xcode environment"),
        (rn_host_root / "android/app/.cxx", "Android CMake output"),
        (rn_host_root / "android/app/build", "Android app build output"),
    )
    for path, reason in generated_host_paths:
        if path.exists() or path.is_symlink():
            _assert_allowed(path, allowed_roots)
            items.append(CleanupItem("rn-host-generated", path, reason))

    if include_archive_local and (
        archive_local_root.exists() or archive_local_root.is_symlink()
    ):
        _assert_allowed(archive_local_root, allowed_roots)
        items.append(
            CleanupItem(
                "archive-local",
                archive_local_root,
                "local copies of already archived sources",
            )
        )

    if include_cargo_target and (
        cargo_target_root.exists() or cargo_target_root.is_symlink()
    ):
        _assert_allowed(cargo_target_root, allowed_roots)
        items.append(
            CleanupItem(
                "cargo-target", cargo_target_root, "rebuildable Rust target cache"
            )
        )

    return items


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
    parser.add_argument("--include-archive-local", action="store_true")
    parser.add_argument("--include-cargo-target", action="store_true")
    args = parser.parse_args(argv)
    try:
        items = build_plan(
            keep_runs=args.keep_runs,
            include_archive_local=args.include_archive_local,
            include_cargo_target=args.include_cargo_target,
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
