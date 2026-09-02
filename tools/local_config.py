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


@dataclass(frozen=True)
class ConfiguredValue:
    value: str | None
    source: str


def _normalized_path(value: str) -> Path:
    expanded = Path(os.path.expandvars(value.strip())).expanduser()
    if not expanded.is_absolute():
        expanded = PROJECT_ROOT / expanded
    return expanded.resolve(strict=False)


def _local_table(name: str) -> dict[str, object]:
    if not LOCAL_CONFIG_PATH.is_file():
        return {}
    with LOCAL_CONFIG_PATH.open("rb") as stream:
        document = tomllib.load(stream)
    table = document.get(name, {})
    if not isinstance(table, dict):
        raise TypeError(f"[{name}] must be a TOML table in {LOCAL_CONFIG_PATH}")
    return table


def _local_toolchains() -> dict[str, object]:
    return _local_table("toolchains")


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


def resolve_local_value(
    table: str,
    key: str,
    environment_variable: str,
    default: str | None,
) -> ConfiguredValue:
    environment_value = os.environ.get(environment_variable)
    if environment_value is not None:
        if not environment_value.strip():
            raise ValueError(
                f"{environment_variable} must be a non-empty value when set"
            )
        return ConfiguredValue(
            environment_value.strip(),
            f"environment variable {environment_variable}",
        )

    local_value = _local_table(table).get(key)
    if local_value is not None:
        if not isinstance(local_value, str):
            raise TypeError(f"{table}.{key} must be a string in {LOCAL_CONFIG_PATH}")
        if not local_value.strip():
            raise ValueError(
                f"{table}.{key} must be a non-empty value in {LOCAL_CONFIG_PATH}"
            )
        return ConfiguredValue(
            local_value.strip(),
            f"{LOCAL_CONFIG_PATH.name} [{table}].{key}",
        )

    return ConfiguredValue(
        default, "built-in default" if default is not None else "unset"
    )


def report() -> None:
    print(f"local_config={LOCAL_CONFIG_PATH}")
    print(f"local_config_exists={str(LOCAL_CONFIG_PATH.is_file()).lower()}")
    for name, environment_variable in (
        ("swift", "BLEHUB_SWIFT_ROOT"),
        ("android", "BLEHUB_ANDROID_ROOT"),
        ("qemu", "BLEHUB_QEMU_ROOT"),
    ):
        configured = resolve_toolchain_root(name, environment_variable)
        print(f"{name}_root={configured.path}")
        print(f"{name}_root_source={configured.source}")

    for key, environment_variable, default in (
        ("executable", "BLEHUB_MUTAGEN_EXE", "mutagen"),
        ("host", "BLEHUB_MUTAGEN_HOST", None),
        ("user", "BLEHUB_MUTAGEN_USER", "vision"),
        ("remote_path", "BLEHUB_MUTAGEN_REMOTE_PATH", "~/projects/blehub"),
        ("session_name", "BLEHUB_MUTAGEN_SESSION_NAME", "blehub"),
    ):
        configured = resolve_local_value("mutagen", key, environment_variable, default)
        value = configured.value if configured.value is not None else "<unset>"
        print(f"mutagen_{key}={value}")
        print(f"mutagen_{key}_source={configured.source}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve BleHub machine-local paths and endpoint settings."
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
