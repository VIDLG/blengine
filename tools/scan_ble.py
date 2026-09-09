from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan nearby Bluetooth LE advertisements with Bleak."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="scan duration in seconds (default: 10)",
    )
    parser.add_argument(
        "--name",
        help="case-insensitive local-name substring to match",
    )
    parser.add_argument(
        "--exact-name",
        action="store_true",
        help="match --name exactly instead of as a substring",
    )
    parser.add_argument(
        "--service-uuid",
        action="append",
        default=[],
        help="require an advertised service UUID; may be repeated",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    parser.add_argument(
        "--require-match",
        action="store_true",
        help="return exit code 1 when no advertisement matches",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.exact_name and not args.name:
        parser.error("--exact-name requires --name")
    return args


def _device_name(device: BLEDevice, advertisement: AdvertisementData) -> str:
    return advertisement.local_name or device.name or ""


def _matches(
    device: BLEDevice,
    advertisement: AdvertisementData,
    *,
    name: str | None,
    exact_name: bool,
    service_uuids: list[str],
) -> bool:
    if name:
        actual_name = _device_name(device, advertisement).casefold()
        expected_name = name.casefold()
        if exact_name:
            if actual_name != expected_name:
                return False
        elif expected_name not in actual_name:
            return False

    advertised_uuids = {value.casefold() for value in advertisement.service_uuids}
    return all(value.casefold() in advertised_uuids for value in service_uuids)


def _hex_bytes(value: bytes) -> str:
    return value.hex(" ")


def _to_record(
    device: BLEDevice,
    advertisement: AdvertisementData,
) -> dict[str, Any]:
    return {
        "name": _device_name(device, advertisement) or None,
        "address": device.address,
        "rssi": advertisement.rssi,
        "tx_power": advertisement.tx_power,
        "service_uuids": sorted(advertisement.service_uuids),
        "service_data": {
            key: _hex_bytes(value)
            for key, value in sorted(advertisement.service_data.items())
        },
        "manufacturer_data": {
            str(key): _hex_bytes(value)
            for key, value in sorted(advertisement.manufacturer_data.items())
        },
    }


def _print_human(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No matching BLE advertisements found.")
        return

    for record in records:
        name = record["name"] or "<unnamed>"
        rssi = record["rssi"]
        tx_power = record["tx_power"]
        print(f"{name} [{record['address']}] RSSI={rssi} dBm TX={tx_power}")

        service_uuids = record["service_uuids"]
        if service_uuids:
            print(f"  service UUIDs: {', '.join(service_uuids)}")

        for company_id, value in record["manufacturer_data"].items():
            print(f"  manufacturer {company_id}: {value}")

        for service_uuid, value in record["service_data"].items():
            print(f"  service data {service_uuid}: {value}")


async def _scan(args: argparse.Namespace) -> list[dict[str, Any]]:
    discovered = await BleakScanner.discover(
        timeout=args.timeout,
        return_adv=True,
        service_uuids=args.service_uuid,
    )
    matches = [
        _to_record(device, advertisement)
        for device, advertisement in discovered.values()
        if _matches(
            device,
            advertisement,
            name=args.name,
            exact_name=args.exact_name,
            service_uuids=args.service_uuid,
        )
    ]
    return sorted(
        matches,
        key=lambda record: (
            (record["name"] or "").casefold(),
            record["address"],
        ),
    )


def main() -> int:
    args = parse_args()
    try:
        records = asyncio.run(_scan(args))
    except BleakError as error:
        print(f"BLE scan failed: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"BLE scan failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("BLE scan interrupted.", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        _print_human(records)
        print(f"Matched advertisements: {len(records)}")

    return 1 if args.require_match and not records else 0


if __name__ == "__main__":
    raise SystemExit(main())
