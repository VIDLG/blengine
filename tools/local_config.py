from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATH = PROJECT_ROOT / "blehub.local.toml"
DEFAULT_TOOLCHAIN_ROOT = Path.home() / ".cache" / "blehub" / "toolchains"


def _shared_qemu_root() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return _normalized_path(local_app_data) / "QEMU"
        return Path.home() / "AppData" / "Local" / "QEMU"
    return Path.home() / ".cache" / "qemu"


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


def _toolchain_override(name: str, environment_variable: str) -> ConfiguredPath | None:
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
    if local_value is None:
        return None
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


def resolve_toolchain_root(name: str, environment_variable: str) -> ConfiguredPath:
    override = _toolchain_override(name, environment_variable)
    if override is not None:
        return override
    return ConfiguredPath(
        (DEFAULT_TOOLCHAIN_ROOT / name).resolve(),
        "portable user-cache default",
    )


def resolve_qemu_root(environment_variable: str) -> ConfiguredPath:
    override = _toolchain_override("qemu", environment_variable)
    if override is not None:
        return override
    return ConfiguredPath(
        _shared_qemu_root().resolve(),
        "shared user QEMU installation default",
    )


def _standard_android_sdk_root() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return (_normalized_path(local_app_data) / "Android" / "Sdk").resolve()
        return (Path.home() / "AppData" / "Local" / "Android" / "Sdk").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Android" / "sdk").resolve()
    return (Path.home() / "Android" / "Sdk").resolve()


def resolve_android_sdk_root() -> ConfiguredPath:
    override = _toolchain_override("android", "BLEHUB_ANDROID_ROOT")
    if override is not None:
        return override

    standard_environment: list[tuple[str, Path]] = []
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(variable)
        if value is None:
            continue
        if not value.strip():
            raise ValueError(f"{variable} must be a non-empty path when set")
        standard_environment.append((variable, _normalized_path(value)))

    if standard_environment:
        selected = standard_environment[0][1]
        conflicting = [
            f"{variable}={path}"
            for variable, path in standard_environment[1:]
            if path != selected
        ]
        if conflicting:
            first_variable, first_path = standard_environment[0]
            values = ", ".join((f"{first_variable}={first_path}", *conflicting))
            raise ValueError(f"Android SDK environment paths disagree: {values}")
        variables = ", ".join(variable for variable, _ in standard_environment)
        return ConfiguredPath(selected, f"environment variable {variables}")

    return ConfiguredPath(
        _standard_android_sdk_root(), "standard shared Android SDK location"
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
    configured_roots = (
        ("android", resolve_android_sdk_root()),
        ("qemu", resolve_qemu_root("BLEHUB_QEMU_ROOT")),
    )
    for name, configured in configured_roots:
        print(f"{name}_root={configured.path}")
        print(f"{name}_root_source={configured.source}")

    for table, key, environment_variable in (
        ("toolchains", "nrftest_root", "BLEHUB_NRFTEST_ROOT"),
        ("peripheral_nrf52840", "port", "BLEHUB_NRF52840_PORT"),
    ):
        configured = resolve_local_value(table, key, environment_variable, None)
        value = configured.value if configured.value is not None else "<unset>"
        print(f"{table}_{key}={value}")
        print(f"{table}_{key}_source={configured.source}")

    for key, environment_variable, default in (
        ("vendor_id", "BLEHUB_PERIPHERAL_USB_VENDOR_ID", None),
        ("product_id", "BLEHUB_PERIPHERAL_USB_PRODUCT_ID", None),
        ("serial", "BLEHUB_PERIPHERAL_USB_SERIAL", None),
        ("qmp_id", "BLEHUB_PERIPHERAL_USB_QMP_ID", "ble-controller"),
        ("bus", "BLEHUB_PERIPHERAL_USB_BUS", "xhci.0"),
    ):
        configured = resolve_local_value(
            "peripheral_usb", key, environment_variable, default
        )
        value = configured.value if configured.value is not None else "<unset>"
        print(f"peripheral_usb_{key}={value}")
        print(f"peripheral_usb_{key}_source={configured.source}")

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
