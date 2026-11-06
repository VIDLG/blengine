"""Stage the native Apple Swift backend independently of its UniFFI image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("platforms/apple/Sources/BleHubApple")
TEMPLATE = Path("platforms/apple/packaging/Package.swift.in")
MARKER = ".blehub-apple-backend-stage.json"
OWNER = "tools.apple.stage_backend"
PLACEHOLDER = "@UNIFFI_PACKAGE_PATH@"


def _swift_string(value: str) -> str:
    """Quote a Swift literal without enabling interpolation or JSON-only escapes."""
    escapes = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return (
        '"'
        + "".join(
            escapes.get(character)
            or (
                f"\\u{{{ord(character):x}}}"
                if ord(character) < 32 or ord(character) in {127, 0x2028, 0x2029}
                else character
            )
            for character in value
        )
        + '"'
    )


def _managed_output(output: Path) -> Path:
    requested = output.expanduser().absolute()
    destination = Path(os.path.abspath(requested))
    managed = ROOT.resolve() / ".work/staged"
    if destination == managed or not destination.is_relative_to(managed):
        raise RuntimeError(
            f"output must be strictly inside managed {managed}: {output}"
        )
    # Check before resolve, including symlinks traversed before a '..' component.
    for path in (requested, *requested.parents):
        if path.is_symlink():
            raise RuntimeError(f"managed output contains a symlink: {path}")
        if path == ROOT.resolve():
            break
    return destination


def _check_owned(output: Path) -> None:
    if not output.exists():
        return
    marker = output / MARKER
    if not output.is_dir() or not marker.is_file() or marker.is_symlink():
        raise RuntimeError(f"refusing to replace output not owned by {OWNER}: {output}")
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise RuntimeError(f"invalid owned output marker: {marker}") from error
    if (
        not isinstance(metadata, dict)
        or metadata.get("owner") != OWNER
        or metadata.get("schema") != 1
        or metadata.get("package") != "BleHubApple"
    ):
        raise RuntimeError(f"refusing output not owned by {OWNER}: {output}")


def stage(uniffi_package: Path, output: Path) -> Path:
    """Copy canonical backend Swift sources into an explicitly owned managed package.

    UniFFI is an existing, read-only package input (production or BLeng). Validate
    every input and prepare the replacement before deleting any previous output.
    The returned path is the absolute staged backend package root.
    """
    destination = _managed_output(output)
    uniffi_package = uniffi_package.expanduser().resolve()
    source = ROOT / SOURCE
    template = ROOT / TEMPLATE
    for input_path in (uniffi_package, source.resolve(), template.resolve()):
        if destination.is_relative_to(input_path) or input_path.is_relative_to(
            destination
        ):
            raise RuntimeError(f"output overlaps an input: {destination}, {input_path}")
    _check_owned(destination)

    manifest = uniffi_package / "Package.swift"
    xcframework = uniffi_package / "BleHubUniFfiNative.xcframework"
    flavor_path = uniffi_package / "BleHubUniFfi.flavor"
    for path in (manifest, flavor_path, template):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"missing or symlinked input: {path}")
    if not xcframework.is_dir() or xcframework.is_symlink():
        raise RuntimeError(f"missing or symlinked UniFFI XCFramework: {xcframework}")
    flavor = flavor_path.read_text(encoding="utf-8").strip()
    if flavor not in {"production", "bleng"}:
        raise RuntimeError(f"unsupported UniFFI package flavor: {flavor!r}")
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError(f"missing or symlinked backend source: {source}")
    source_files: list[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"backend source contains a symlink: {path}")
        if path.is_file() and path.suffix == ".swift":
            source_files.append(path.relative_to(source))
    if not source_files:
        raise RuntimeError(f"backend source has no Swift files: {source}")
    template_text = template.read_text(encoding="utf-8")
    if template_text.count(PLACEHOLDER) != 1:
        raise RuntimeError(f"backend package template must contain one {PLACEHOLDER}")
    relative_uniffi = Path(os.path.relpath(uniffi_package, destination)).as_posix()
    rendered = template_text.replace(PLACEHOLDER, _swift_string(relative_uniffi))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}-", dir=destination.parent
    ) as temporary:
        prepared = Path(temporary)
        staged_source = prepared / "Sources/BleHubApple"
        staged_source.mkdir(parents=True)
        source_hashes: dict[str, str] = {}
        for relative in source_files:
            copied = staged_source / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, copied)
            source_hashes[relative.as_posix()] = hashlib.sha256(
                copied.read_bytes()
            ).hexdigest()
        (prepared / "Package.swift").write_text(
            rendered, encoding="utf-8", newline="\n"
        )
        metadata = {
            "schema": 1,
            "owner": OWNER,
            "package": "BleHubApple",
            "source": SOURCE.as_posix(),
            "source_sha256": source_hashes,
            "template": TEMPLATE.as_posix(),
            "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
            "uniffi_package": relative_uniffi,
            "uniffi_flavor": flavor,
            "uniffi_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        }
        (prepared / MARKER).write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # Recheck ownership immediately before replacement; never clear an input
        # or an unmarked directory just because it is somewhere under staging.
        _managed_output(destination)
        _check_owned(destination)
        if destination.exists():
            shutil.rmtree(destination)
        prepared.replace(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uniffi-package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        destination = stage(arguments.uniffi_package, arguments.output)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"apple_backend_stage_error={error}\n")
    print(f"apple_backend_package={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
