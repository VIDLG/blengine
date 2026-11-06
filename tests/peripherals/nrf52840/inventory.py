"""Read-only inventory of configured nRF52840 DK J-Link identities."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.local_config import LOCAL_CONFIG_PATH

from tools.ble_sniffer import enumerate_ports

from .jlink_probe import EnumeratedProbe, ProbeDetectionError, enumerate_probes


@dataclass(frozen=True)
class ConfiguredDevice:
    """One device entry from the machine-local peripheral configuration."""

    name: str
    probe_serial: str
    port: str | None
    profile: str | None


def _canonical_serial(value: str) -> str:
    if not value.isdigit() or not value:
        raise ValueError("probe_serial must be a non-empty decimal string")
    return value.lstrip("0") or "0"


def load_configured_devices(path: Path = LOCAL_CONFIG_PATH) -> tuple[ConfiguredDevice, ...]:
    """Load device identities without assuming names or a fixed device count."""
    if not path.is_file():
        raise ValueError(f"missing local configuration: {path}")
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    peripheral = document.get("peripheral_nrf52840", {})
    if not isinstance(peripheral, dict):
        raise ValueError("peripheral_nrf52840 must be a TOML table")
    devices = peripheral.get("devices", {})
    if not isinstance(devices, dict) or not devices:
        raise ValueError("peripheral_nrf52840.devices must contain at least one device")

    result: list[ConfiguredDevice] = []
    serials: set[str] = set()
    for name, raw in sorted(devices.items()):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("configured device names must be non-empty strings")
        if not isinstance(raw, dict):
            raise ValueError(f"peripheral_nrf52840.devices.{name} must be a TOML table")
        serial = raw.get("probe_serial")
        port = raw.get("port")
        profile = raw.get("profile")
        if not isinstance(serial, str):
            raise ValueError(f"peripheral_nrf52840.devices.{name}.probe_serial must be a string")
        canonical_serial = _canonical_serial(serial)
        if canonical_serial in serials:
            raise ValueError(f"duplicate configured probe_serial: {serial}")
        serials.add(canonical_serial)
        if port is not None and not isinstance(port, str):
            raise ValueError(f"peripheral_nrf52840.devices.{name}.port must be a string")
        if profile is not None and not isinstance(profile, str):
            raise ValueError(f"peripheral_nrf52840.devices.{name}.profile must be a string")
        result.append(ConfiguredDevice(name, serial, port, profile))
    return tuple(result)


def _configured_sniffer_port(path: Path) -> str | None:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    peripheral = document.get("peripheral_nrf52840", {})
    if not isinstance(peripheral, dict):
        raise ValueError("peripheral_nrf52840 must be a TOML table")
    value = peripheral.get("sniffer_port")
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("peripheral_nrf52840.sniffer_port must be a non-empty string")
    return value.strip() if isinstance(value, str) else None


def _probe_document(probe: EnumeratedProbe) -> dict[str, Any]:
    return {
        "probe_serial": probe.serial_number,
        "pid": probe.pid,
        "location": probe.location,
        "ports": list(probe.ports),
    }


def inspect(path: Path = LOCAL_CONFIG_PATH) -> dict[str, Any]:
    """Return the configured-to-observed J-Link relationship."""
    configured = load_configured_devices(path)
    observed = enumerate_probes()
    by_serial = {probe.serial_number: probe for probe in observed}
    expected_serials = {_canonical_serial(device.probe_serial) for device in configured}
    devices: list[dict[str, Any]] = []
    for device in configured:
        serial = _canonical_serial(device.probe_serial)
        probe = by_serial.get(serial)
        devices.append(
            {
                "name": device.name,
                "expected_probe_serial": device.probe_serial,
                "configured_port": device.port,
                "profile": device.profile,
                "connected": probe is not None,
                "observed": None if probe is None else _probe_document(probe),
                "configured_port_status": (
                    "missing-device"
                    if probe is None
                    else "match"
                    if device.port in probe.ports
                    else "drift"
                ),
            }
        )
    unconfigured = [
        _probe_document(probe)
        for probe in observed
        if probe.serial_number not in expected_serials
    ]
    sniffer_port = _configured_sniffer_port(path)
    serial_ports = enumerate_ports()
    sniffer_observed = next(
        (port for port in serial_ports if port["device"] == sniffer_port), None
    ) if sniffer_port is not None else None
    sniffer_status = "not-configured"
    if sniffer_port is not None:
        if sniffer_observed is None:
            sniffer_status = "missing"
        elif "sniffer" not in str(sniffer_observed.get("description", "")).lower():
            sniffer_status = "wrong-device"
        else:
            sniffer_status = "match"
    connected = all(device["connected"] for device in devices)
    port_drift = any(device["configured_port_status"] == "drift" for device in devices)
    topology_ready = connected and sniffer_status in {"match", "not-configured"}
    return {
        "schema_version": 1,
        "config_path": str(path),
        "status": (
            "passed-port-drift"
            if topology_ready and port_drift
            else "passed"
            if topology_ready
            else "failed"
        ),
        "configured_device_count": len(devices),
        "observed_probe_count": len(observed),
        "devices": devices,
        "unconfigured_probes": unconfigured,
        "sniffer": {
            "configured_port": sniffer_port,
            "status": sniffer_status,
            "observed": sniffer_observed,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=LOCAL_CONFIG_PATH)
    args = parser.parse_args(argv)
    try:
        document = inspect(args.config)
    except (OSError, ProbeDetectionError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"nrf52840_inventory=failed error={error}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if document["status"] in {"passed", "passed-port-drift"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
