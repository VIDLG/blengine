from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "conformance"
    / "scenarios"
    / "basic-gatt-peripheral.json"
)

PROPERTY_NAMES = {
    "read": "read",
    "write": "write",
    "write_without_response": "write-without-response",
    "notify": "notify",
    "indicate": "indicate",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a physical BLE peripheral against a fixture profile."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"fixture JSON path (default: {DEFAULT_FIXTURE})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="discovery and connection timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--address",
        help="select a specific discovered device address",
    )
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="verify advertising and GATT structure without read/write/subscribe operations",
    )
    parser.add_argument(
        "--notification-timeout",
        type=float,
        default=0.0,
        help=(
            "keep each notify/indicate subscription open until one update arrives "
            "or this many seconds elapse (default: do not wait)"
        ),
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.notification_timeout < 0:
        parser.error("--notification-timeout must not be negative")
    if args.structure_only and args.notification_timeout > 0:
        parser.error("--notification-timeout cannot be used with --structure-only")
    return args


def _uuid_key(value: str) -> str:
    return value.replace("-", "").casefold()


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load fixture {path}: {error}") from error

    if fixture.get("schema_version") != 1:
        raise ValueError("fixture schema_version must be 1")
    if not fixture.get("advertising", {}).get("service_uuids"):
        raise ValueError("fixture must advertise at least one service UUID")
    if not fixture.get("services"):
        raise ValueError("fixture must define at least one GATT service")
    return fixture


def _device_name(device: BLEDevice, advertisement: AdvertisementData) -> str:
    return advertisement.local_name or device.name or ""


async def _discover(
    fixture: dict[str, Any],
    *,
    timeout: float,
    address: str | None,
) -> tuple[BLEDevice, AdvertisementData]:
    required_uuids = {
        _uuid_key(value) for value in fixture["advertising"]["service_uuids"]
    }
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches = [
        (device, advertisement)
        for device, advertisement in discovered.values()
        if required_uuids.issubset(
            {_uuid_key(value) for value in advertisement.service_uuids}
        )
        and (address is None or device.address.casefold() == address.casefold())
    ]

    if not matches:
        qualifier = f" at address {address}" if address else ""
        raise RuntimeError("no advertiser matched fixture service UUIDs" + qualifier)
    if len(matches) > 1:
        addresses = ", ".join(sorted(device.address for device, _ in matches))
        raise RuntimeError(
            f"multiple fixture advertisers found ({addresses}); use --address"
        )
    return matches[0]


def _find_service(services: Any, uuid: str) -> Any | None:
    expected = _uuid_key(uuid)
    return next(
        (service for service in services if _uuid_key(service.uuid) == expected),
        None,
    )


def _find_characteristic(
    service: Any,
    uuid: str,
) -> BleakGATTCharacteristic | None:
    expected = _uuid_key(uuid)
    return next(
        (
            characteristic
            for characteristic in service.characteristics
            if _uuid_key(characteristic.uuid) == expected
        ),
        None,
    )


def _verify_properties(
    characteristic: BleakGATTCharacteristic,
    expected_properties: list[str],
) -> None:
    expected = {PROPERTY_NAMES[value] for value in expected_properties}
    actual = set(characteristic.properties)
    missing = expected - actual
    if missing:
        raise RuntimeError(
            f"characteristic {characteristic.uuid} is missing properties: "
            + ", ".join(sorted(missing))
        )


async def _verify_operations(
    client: BleakClient,
    characteristic: BleakGATTCharacteristic,
    definition: dict[str, Any],
    *,
    notification_timeout: float,
) -> list[str]:
    properties = set(definition["properties"])
    role = definition.get("role", characteristic.uuid)
    completed: list[str] = []

    initial_hex = definition.get("initial_value_hex")
    initial = bytes.fromhex(initial_hex) if initial_hex is not None else None
    if "read" in properties:
        actual = bytes(await client.read_gatt_char(characteristic))
        completed.append(f"read={actual.hex()}")
        if initial is not None and actual != initial:
            raise RuntimeError(
                f"{role} initial value mismatch: expected {initial.hex()}, "
                f"received {actual.hex()}"
            )

    if "write" in properties:
        await client.write_gatt_char(characteristic, b"\xa5\x5a", response=True)
        completed.append("write")

    if "write_without_response" in properties:
        await client.write_gatt_char(characteristic, b"\x5a\xa5", response=False)
        completed.append("write-without-response")

    if "notify" in properties or "indicate" in properties:
        notification_received = asyncio.Event()
        notification_value: bytes | None = None

        def on_notification(_characteristic: Any, data: bytearray) -> None:
            nonlocal notification_value
            notification_value = bytes(data)
            print(
                f"notification_received role={role} "
                f"uuid={characteristic.uuid} value={notification_value.hex()}",
                flush=True,
            )
            notification_received.set()

        await client.start_notify(characteristic, on_notification)
        try:
            if notification_timeout > 0:
                print(
                    f"notification_subscription=ready role={role} "
                    f"uuid={characteristic.uuid} timeout={notification_timeout:g}s",
                    flush=True,
                )
                try:
                    await asyncio.wait_for(
                        notification_received.wait(), timeout=notification_timeout
                    )
                except TimeoutError as error:
                    raise RuntimeError(
                        f"{role} subscription succeeded but no notification arrived "
                        f"within {notification_timeout:g} seconds"
                    ) from error
        finally:
            await client.stop_notify(characteristic)

        if notification_timeout > 0:
            assert notification_value is not None
            completed.append(f"notification={notification_value.hex()}")
        else:
            completed.append("subscribe/unsubscribe")

    if initial is not None and "write" in properties:
        await client.write_gatt_char(characteristic, initial, response=True)
        completed.append("restore")

    return completed


async def _verify(args: argparse.Namespace) -> None:
    fixture = _load_fixture(args.fixture)
    device, advertisement = await _discover(
        fixture,
        timeout=args.timeout,
        address=args.address,
    )
    actual_name = _device_name(device, advertisement) or "<unnamed>"
    expected_name = fixture["advertising"].get("local_name")
    print(f"advertising=passed address={device.address} name={actual_name}")
    if expected_name and actual_name != expected_name:
        message = f"expected={expected_name} actual={actual_name}"
        if fixture["advertising"].get("require_local_name", False):
            raise RuntimeError(f"advertising name mismatch: {message}")
        print(f"advertising_name=warning {message}")

    async with BleakClient(device, timeout=args.timeout) as client:
        print("connection=passed")
        for service_definition in fixture["services"]:
            service = _find_service(client.services, service_definition["uuid"])
            if service is None:
                raise RuntimeError(f"missing service {service_definition['uuid']}")
            print(f"service=passed uuid={service.uuid}")

            for definition in service_definition["characteristics"]:
                characteristic = _find_characteristic(service, definition["uuid"])
                if characteristic is None:
                    raise RuntimeError(f"missing characteristic {definition['uuid']}")
                _verify_properties(characteristic, definition["properties"])
                message = (
                    f"characteristic=passed role={definition.get('role')} "
                    f"uuid={characteristic.uuid} "
                    f"properties={','.join(characteristic.properties)}"
                )
                if not args.structure_only:
                    operations = await _verify_operations(
                        client,
                        characteristic,
                        definition,
                        notification_timeout=args.notification_timeout,
                    )
                    if operations:
                        message += f" operations={','.join(operations)}"
                print(message)

    print(f"fixture={fixture['profile_id']} status=passed")
    if not args.structure_only:
        if args.notification_timeout > 0:
            print("notification_delivery=passed")
        else:
            print(
                "notification_delivery=not-tested "
                "(rerun with --notification-timeout and trigger an update while subscribed)"
            )


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(_verify(args))
    except (BleakError, OSError, RuntimeError, ValueError) as error:
        print(f"fixture verification failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("fixture verification interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
