from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATH = PROJECT_ROOT / "blehub.local.toml"
DEFAULT_TOOLCHAIN_ROOT = Path.home() / ".cache" / "blehub" / "toolchains"


@dataclass(frozen=True)
class ConfiguredPath:
    path: Path
    source: str


def _normalized_path(value: str) -> Path:
    expanded = Path(os.path.expandvars(value.strip())).expanduser()
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return expanded.resolve(strict=False)


def _local_toolchains() -> dict[str, object]:
    if not LOCAL_CONFIG_PATH.is_file():
        return {}
    with LOCAL_CONFIG_PATH.open("rb") as stream:
        document = tomllib.load(stream)
    toolchains = document.get("toolchains", {})
    if not isinstance(toolchains, dict):
        raise TypeError(f"[toolchains] must be a TOML table in {LOCAL_CONFIG_PATH}")
    return toolchains


def resolve_toolchain_root(name: str, environment_variable: str) -> ConfiguredPath:
    environment_value = os.environ.get(environment_variable)
    if environment_value is not None:
        if not environment_value.strip():
            raise ValueError(
                f"{environment_variable} must be a non-empty path when set"
            )
        return ConfiguredPath(
            _normalized_path(environment_value),
            f"environment variable {environment_variable}",
        )

    key = f"{name}_root"
    local_value = _local_toolchains().get(key)
    if local_value is not None:
        if not isinstance(local_value, str):
            raise TypeError(f"toolchains.{key} must be a string in {LOCAL_CONFIG_PATH}")
        if not local_value.strip():
            raise ValueError(
                f"toolchains.{key} must be a non-empty path in {LOCAL_CONFIG_PATH}"
            )
        return ConfiguredPath(
            _normalized_path(local_value),
            f"{LOCAL_CONFIG_PATH.name} [toolchains].{key}",
        )

    return ConfiguredPath(
        (DEFAULT_TOOLCHAIN_ROOT / name).resolve(),
        "portable user-cache default",
    )


def report() -> None:
    print(f"local_config={LOCAL_CONFIG_PATH}")
    print(f"local_config_exists={str(LOCAL_CONFIG_PATH.is_file()).lower()}")
    for name, environment_variable in (
        ("swift", "BLEHUB_SWIFT_ROOT"),
        ("android", "BLEHUB_ANDROID_ROOT"),
    ):
        configured = resolve_toolchain_root(name, environment_variable)
        print(f"{name}_root={configured.path}")
        print(f"{name}_root_source={configured.source}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve BleHub machine-local toolchain paths."
    )
    parser.add_argument("command", choices=("report",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "report":
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
