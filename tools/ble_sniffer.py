"""Capture BLE air traffic with the independently managed nRF52840 sniffer."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scapy.all import load_contrib, rdpcap
from scapy.contrib.nrf_sniffer import NRF2_Packet_Event, NRFS2_Packet
from scapy.error import Scapy_Exception
from scapy.layers.bluetooth import (
    ATT_Handle_Value_Notification,
    ATT_Hdr,
    ATT_Write_Request,
)
from scapy.layers.bluetooth4LE import BTLE_ADV, BTLE_DATA
from serial.tools import list_ports as serial_list_ports

from tools.local_config import PROJECT_ROOT, resolve_local_value


class BleSnifferError(RuntimeError):
    """Raised when a bounded sniffer capture cannot be started or completed."""


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".work" / "sniffer"


def _non_empty(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def resolve_port(argument: str | None = None) -> str:
    value = _non_empty(argument)
    if value is None:
        configured = resolve_local_value(
            "peripheral_nrf52840",
            "sniffer_port",
            "BLEHUB_NRF52840_SNIFFER_PORT",
            None,
        )
        value = _non_empty(configured.value)
    if value is None:
        raise BleSnifferError(
            "sniffer port is unset; pass --port or configure "
            "peripheral_nrf52840.sniffer_port / BLEHUB_NRF52840_SNIFFER_PORT"
        )
    return value


def resolve_nrfutil_home(argument: str | None = None) -> Path:
    value = _non_empty(argument)
    if value is None:
        configured = resolve_local_value(
            "tools", "nrfutil_home", "BLEHUB_NRFUTIL_HOME", None
        )
        value = _non_empty(configured.value)
    if value is None:
        raise BleSnifferError(
            "nrfutil home is unset; pass --nrfutil-home or configure "
            "tools.nrfutil_home / BLEHUB_NRFUTIL_HOME"
        )
    home = Path(os.path.expandvars(value)).expanduser()
    if not home.is_absolute():
        home = PROJECT_ROOT / home
    return home.resolve(strict=False)


def resolve_output(argument: str | None) -> Path:
    value = _non_empty(argument)
    if value is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return DEFAULT_OUTPUT_ROOT / f"ble-capture-{timestamp}.pcap"
    output = Path(os.path.expandvars(value)).expanduser()
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    return output.resolve(strict=False)


def nrfutil_executable(home: Path) -> Path:
    executable = home / "bin" / ("nrfutil.exe" if os.name == "nt" else "nrfutil")
    if not executable.is_file():
        raise BleSnifferError(f"nrfutil executable is missing: {executable}")
    return executable


def build_command(
    executable: Path,
    port: str,
    output: Path,
    *,
    follow: str | None = None,
    follow_by_name: str | None = None,
    only_advertising: bool = False,
    scan_follow_rsp: bool = False,
) -> list[str]:
    command = [
        str(executable),
        "ble-sniffer",
        "sniff",
        "--port",
        port,
        "--output-pcap-file",
        str(output),
    ]
    if follow is not None:
        command.extend(("--follow", follow))
    if follow_by_name is not None:
        command.extend(("--follow-by-name", follow_by_name))
    if only_advertising:
        command.append("--only-advertising")
    if scan_follow_rsp:
        command.append("--scan-follow-rsp")
    return command


def metadata_path(output: Path) -> Path:
    return output.with_name(output.name + ".json")


def inspect_pcap_devices(path: Path) -> list[dict[str, object]]:
    """Extract advertising addresses/names through Scapy's Nordic BLE dissector."""
    try:
        load_contrib("nrf_sniffer")
        packets = rdpcap(str(path))
    except Exception as error:
        raise BleSnifferError(
            f"unable to dissect Nordic BLE PCAP {path}: {error}"
        ) from error

    devices: dict[str, dict[str, object]] = {}
    for packet in packets:
        nrf_packet = packet.getlayer(NRFS2_Packet)
        if nrf_packet is None or nrf_packet.payload is None:
            continue
        try:
            event = NRF2_Packet_Event(bytes(nrf_packet.payload))
        except (IndexError, Scapy_Exception, TypeError, ValueError):
            continue
        advertising = event.getlayer(BTLE_ADV)
        if advertising is None:
            continue
        address_layer = advertising.payload
        address = getattr(address_layer, "AdvA", None)
        if not isinstance(address, str):
            continue
        record = devices.setdefault(
            address,
            {
                "address": address,
                "packets": 0,
                "pdu_types": set(),
                "names": set(),
                "service_uuids": set(),
            },
        )
        record["packets"] = int(record["packets"]) + 1
        pdu_types = record["pdu_types"]
        assert isinstance(pdu_types, set)
        pdu_types.add(str(getattr(advertising, "PDU_type", "unknown")))
        for data in getattr(address_layer, "data", []) or []:
            payload = getattr(data, "payload", None)
            name = getattr(payload, "local_name", None)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            if isinstance(name, str):
                names = record["names"]
                assert isinstance(names, set)
                names.add(name)
            service_uuids = getattr(payload, "svc_uuids", None)
            if isinstance(service_uuids, list):
                observed = record["service_uuids"]
                assert isinstance(observed, set)
                observed.update(str(value).casefold() for value in service_uuids)
    return [
        {
            "address": record["address"],
            "packets": record["packets"],
            "pdu_types": sorted(record["pdu_types"]),
            "names": sorted(record["names"]),
            "service_uuids": sorted(record["service_uuids"]),
        }
        for record in sorted(devices.values(), key=lambda value: str(value["address"]))
    ]


def inspect_pcap_att(path: Path) -> dict[str, object]:
    """Summarize ATT writes/notifications using Scapy's BLE dissector."""
    try:
        load_contrib("nrf_sniffer")
        packets = rdpcap(str(path))
    except Exception as error:
        raise BleSnifferError(
            f"unable to dissect Nordic BLE PCAP {path}: {error}"
        ) from error

    events: list[dict[str, object]] = []
    data_packets = 0
    for index, packet in enumerate(packets):
        nrf_packet = packet.getlayer(NRFS2_Packet)
        if nrf_packet is None or nrf_packet.payload is None:
            continue
        try:
            event = NRF2_Packet_Event(bytes(nrf_packet.payload))
        except (IndexError, Scapy_Exception, TypeError, ValueError):
            continue
        if event.getlayer(BTLE_DATA) is not None:
            data_packets += 1
        att = event.getlayer(ATT_Hdr)
        if att is None:
            continue
        row: dict[str, object] = {
            "packet_index": index,
            "opcode": int(att.opcode),
            "direction": event.fields.get("direction"),
            "event_counter": event.fields.get("event_counter"),
        }
        notification = event.getlayer(ATT_Handle_Value_Notification)
        if notification is not None:
            row.update(
                {
                    "kind": "notification",
                    "handle": notification.gatt_handle,
                    "value_hex": bytes(notification.value).hex(),
                }
            )
        write = event.getlayer(ATT_Write_Request)
        if write is not None:
            row.update(
                {
                    "kind": "write-request",
                    "handle": write.gatt_handle,
                    "value_hex": bytes(write.data).hex(),
                }
            )
        events.append(row)
    return {
        "pcap": str(path),
        "packets": len(packets),
        "data_packets": data_packets,
        "att_events": events,
    }


def _write_metadata(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _stop_process(process: subprocess.Popen[bytes], *, interrupt: bool) -> int:
    if process.poll() is not None:
        return int(process.returncode)
    if os.name == "nt":
        event = signal.CTRL_BREAK_EVENT if interrupt else signal.SIGTERM
        process.send_signal(event)
    else:
        os.killpg(process.pid, signal.SIGINT if interrupt else signal.SIGTERM)
    try:
        return int(process.wait(timeout=15))
    except subprocess.TimeoutExpired:
        process.kill()
        return int(process.wait(timeout=15))


def capture(
    *,
    port: str | None = None,
    output: str | None = None,
    nrfutil_home: str | None = None,
    duration: float | None = None,
    follow: str | None = None,
    follow_by_name: str | None = None,
    only_advertising: bool = False,
    scan_follow_rsp: bool = False,
) -> Path:
    """Run nrfutil until Ctrl-C or an optional duration and publish capture metadata."""
    selected_port = resolve_port(port)
    if not Path(selected_port).exists():
        raise BleSnifferError(
            f"configured sniffer port does not exist: {selected_port}"
        )
    home = resolve_nrfutil_home(nrfutil_home)
    executable = nrfutil_executable(home)
    output_path = resolve_output(output)
    if output_path.exists() or output_path.is_symlink():
        raise BleSnifferError(f"refusing to overwrite existing PCAP: {output_path}")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise BleSnifferError("duration must be a finite positive number")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(
        executable,
        selected_port,
        output_path,
        follow=_non_empty(follow),
        follow_by_name=_non_empty(follow_by_name),
        only_advertising=only_advertising,
        scan_follow_rsp=scan_follow_rsp,
    )
    environment = dict(os.environ)
    environment["NRFUTIL_HOME"] = str(home)
    environment["PATH"] = str(home / "bin") + os.pathsep + environment.get("PATH", "")
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    status = "failed"
    returncode: int | None = None
    error: str | None = None
    try:
        process_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, env=environment, **process_kwargs)
        if duration is None:
            returncode = process.wait()
            status = "completed" if returncode == 0 else "failed"
        else:
            try:
                returncode = process.wait(timeout=duration)
                status = "completed" if returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                returncode = _stop_process(process, interrupt=True)
                status = "duration-completed" if output_path.is_file() else "failed"
    except KeyboardInterrupt:
        if process is not None:
            returncode = _stop_process(process, interrupt=True)
        status = "interrupted"
    except OSError as exc:
        error = str(exc)
    finally:
        if process is not None and process.poll() is None:
            returncode = _stop_process(process, interrupt=False)
        metadata = {
            "schema_version": 1,
            "status": status,
            "error": error,
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "duration_seconds": time.monotonic() - started_monotonic,
            "port": selected_port,
            "nrfutil_home": str(home),
            "executable": str(executable),
            "command": command,
            "returncode": returncode,
            "pcap": str(output_path),
            "pcap_exists": output_path.is_file(),
        }
        _write_metadata(metadata_path(output_path), metadata)
    if error is not None:
        raise BleSnifferError(f"unable to start nrfutil: {error}")
    if status == "failed":
        raise BleSnifferError(
            f"nrfutil sniffer exited with code {returncode}; metadata: {metadata_path(output_path)}"
        )
    print(f"BLE sniffer capture: {output_path}")
    print(f"BLE sniffer metadata: {metadata_path(output_path)}")
    return output_path


def enumerate_ports() -> tuple[dict[str, Any], ...]:
    """Return the current serial topology without selecting or opening a port."""
    return tuple(
        {
            "device": port.device,
            "description": port.description,
            "serial": port.serial_number,
            "vid": port.vid,
            "pid": port.pid,
            "location": port.location,
            "manufacturer": port.manufacturer,
        }
        for port in sorted(serial_list_ports.comports(), key=lambda item: item.device)
    )


def list_ports() -> None:
    """Print current serial devices so a newly attached sniffer can be selected."""
    for port in enumerate_ports():
        print(json.dumps(port, sort_keys=True))


class SnifferArguments(argparse.Namespace):
    command: str = ""
    port: str | None = None
    output: str | None = None
    nrfutil_home: str | None = None
    duration: float | None = None
    follow: str | None = None
    follow_by_name: str | None = None
    only_advertising: bool = False
    scan_follow_rsp: bool = False
    pcap: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--port")
    capture_parser.add_argument("--output-pcap-file", dest="output")
    capture_parser.add_argument("--nrfutil-home")
    capture_parser.add_argument(
        "--duration", type=float, help="stop cleanly after this many seconds"
    )
    capture_parser.add_argument("--follow")
    capture_parser.add_argument("--follow-by-name")
    capture_parser.add_argument("--only-advertising", action="store_true")
    capture_parser.add_argument("--scan-follow-rsp", action="store_true")
    _ = subparsers.add_parser("ports", help="list live serial ports as JSON")
    pcap_parser = subparsers.add_parser("pcap-devices")
    pcap_parser.add_argument("--pcap", required=True)
    att_parser = subparsers.add_parser("pcap-att")
    att_parser.add_argument("--pcap", required=True)
    return parser


def main() -> int:
    arguments = SnifferArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        if arguments.command == "ports":
            list_ports()
        elif arguments.command == "pcap-devices":
            print(json.dumps(inspect_pcap_devices(Path(arguments.pcap)), indent=2))
        elif arguments.command == "pcap-att":
            print(json.dumps(inspect_pcap_att(Path(arguments.pcap)), indent=2))
        else:
            capture(
                port=arguments.port,
                output=arguments.output,
                nrfutil_home=arguments.nrfutil_home,
                duration=arguments.duration,
                follow=arguments.follow,
                follow_by_name=arguments.follow_by_name,
                only_advertising=arguments.only_advertising,
                scan_follow_rsp=arguments.scan_follow_rsp,
            )
    except BleSnifferError as error:
        print(f"BLE sniffer error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
