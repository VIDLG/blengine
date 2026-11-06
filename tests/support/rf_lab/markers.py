"""Synthetic RN/Peripheral marker samples; never hardware or RF evidence."""

from __future__ import annotations

import json

from tests.peripherals.nrf52840 import cancellation_control as control_api
from tools.react_native import watchdog_ledger as ledger

SERVICE = "fd000000-0000-4000-8000-000000000000"

MARKERS = [
    "rf:listener:registered-before-initialize",
    "rf:step:initialize:passed:commandId=9001:adapterState=PoweredOn:scanAuthorization=Authorized",
    "rf:step:scan-start:passed:startCommandId=9002:scanSessionId=9004",
    f"rf:step:advertisement:passed:scanSessionId=9004:peripheralId=42:localName=missing:filterServiceUuid={SERVICE}:payloadAdvertisedServices=none:payloadContainsFilterUuid=false",
    "rf:step:scan-stop:passed:stopCommandId=9003:scanSessionId=9004:requested=true",
    "rf:step:silence:passed:scanSessionId=9004:observationMs=2000",
    "rf:step:close:passed:probe-closed=true:listener-removed=true",
    f"rf:passed:filterServiceUuid={SERVICE}:payloadAdvertisedServices=none:payloadContainsFilterUuid=false:scanSessionId=9004:peripheralId=42:startCommandId=9002:stopCommandId=9003",
]


def log(markers: list[str]) -> str:
    return "\n".join(f"BLEHUB_NITRO_RUNTIME {marker}" for marker in markers)


GATT_MARKERS = [
    *MARKERS[:-2],
    f"rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:gatt-discovery:passed:commandId=9008:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:gattSessionId=9009:requestedGattSessionId=9009:serviceCount=1:discoveredServiceCount=2:characteristicCount=5:descriptorCount=2",
    f"rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connection-silence:passed:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    MARKERS[-2],
    f"rf-gatt:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:disconnectCommandId=9006:scanSessionId=9004",
]

IO_MARKERS = [
    *GATT_MARKERS[:9],
    f"rf:step:read-initial:passed:commandId=9010:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid={SERVICE}:characteristicId=fff10000:value=00",
    f"rf:step:write-response:passed:commandId=9011:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid={SERVICE}:characteristicId=fff10000:value=10",
    f"rf:step:read-response:passed:commandId=9012:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid={SERVICE}:characteristicId=fff10000:value=10",
    f"rf:step:write-without-response:passed:commandId=9013:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid={SERVICE}:characteristicId=fff10000:value=ab:terminal=local-submission",
    f"rf:step:read-without-response:passed:commandId=9014:connectionId=9007:gattSessionId=9009:peripheralId=42:serviceUuid={SERVICE}:characteristicId=fff10000:value=ab",
    *GATT_MARKERS[9:-1],
    f"rf-io:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:readCommandId=9010:writeResponseCommandId=9011:writeWithoutResponseCommandId=9013:disconnectCommandId=9006:scanSessionId=9004",
]

CANCELLATION_EVENTS = "rf-cancellation:event:kind=characteristicRead|commandCompleted|commandCompleted|characteristicRead|commandCompleted:commandId=9027|9027|9028|9029|9029:connectionId=9007|none|none|9007|none:gattSessionId=9009|none|none|9009|none:characteristicId=51|none|none|51|none:error=ok|ok|cancelled|ok|ok:value=00|none|none|10|none"

CANCELLATION_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:cancellation-active-rejection:passed:targetCommandId=9027:category=operation_not_cancellable",
    "rf:step:cancellation-queued-accepted:passed:targetCommandId=9028",
    "rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled",
    "rf:step:cancellation-active-completion:passed:commandId=9027",
    "rf:step:cancellation-late-silence:passed:commandId=9028:observationMs=1000",
    "rf:step:cancellation-unknown-target:passed:targetCommandId=9030:category=operation_not_found",
    "rf:step:cancellation-completed-target:passed:targetCommandId=9027:category=operation_not_found",
    "rf:step:cancellation-recovery:passed:commandId=9029",
    *GATT_MARKERS[9:-1],
    CANCELLATION_EVENTS,
    f"rf-cancellation:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:disconnectCommandId=9006:scanSessionId=9004",
]


def controlled_markers(markers: list[str], control: control_api.Control) -> list[str]:
    """Upgrade the historical JS ledger fixture to the held-read protocol."""
    result = [control.marker("listener", "registered")]
    source_ledger = next(
        line for line in markers if line.startswith("rf-cancellation:event:")
    )
    fields = []
    for field in source_ledger.split(":")[2:]:
        name, values = field.split("=", 1)
        items = values.split("|")
        if name == "value":
            items[3] = "00"
        fields.append(name + "=" + "|".join(items[index] for index in (2, 0, 1, 3, 4)))
    ledger = "rf-cancellation:event:" + ":".join(fields)
    for line in markers:
        if line.startswith("rf-cancellation:event:"):
            continue
        if line.startswith("rf:step:close:"):
            result.append(ledger)
        result.append(line)
        if line.startswith("rf:step:gatt-discovery:"):
            result.extend(
                [
                    control.marker("ready", "arm"),
                    control.marker("observed", "armed"),
                    control.marker("ready", "read-dispatched"),
                    control.marker("observed", "held"),
                ]
            )
        if line.startswith("rf:step:cancellation-terminal:"):
            result.extend(
                [
                    control.marker("ready", "release"),
                    control.marker("observed", "released"),
                ]
            )
    return result


def marker(body: str) -> str:
    return ledger.PREFIX + body


def event(kind: str, command: str | None = None, **fields: object) -> str:
    return marker(
        "rf-watchdog:event:"
        + json.dumps(
            {"kind": kind, **({"commandId": command} if command else {}), **fields}
        )
    )


def completed(command: str, error: object = None) -> str:
    return event("commandCompleted", command, **({"error": error} if error else {}))


def ui_database(first_characteristic: str) -> list[dict[str, object]]:
    """Mirror the finished UI test's five-characteristic JSON projection."""
    characteristics = []
    for index, prefix in enumerate(
        ("fff10000", "fff20000", "fff50000", "fff30000", "fff40000")
    ):
        characteristics.append(
            {
                "id": str(int(first_characteristic) + index),
                "uuid": f"{prefix}-0000-4000-8000-000000000000",
                "readable": index != 4,
                "writableWithResponse": index in (0, 4),
                "writableWithoutResponse": index in (0, 4),
                "notifiable": index == 1,
                "indicatable": index in (1, 2),
                "descriptors": [
                    {
                        "id": str(40 + index),
                        "uuid": "00002902-0000-1000-8000-00805f9b34fb",
                    }
                ]
                if index in (1, 2)
                else [],
            }
        )
    return [
        {
            "id": "29",
            "uuid": ledger.SERVICE,
            "primary": True,
            "characteristics": characteristics,
        }
    ]


def log_parts(android: bool = False) -> tuple[str, str, str]:
    """Mirror rf_watchdog.tsx and its fake provider, including reordered terminals."""
    setup = [
        event(
            "adapterSnapshotChanged",
            "21001",
            adapterState="PoweredOn",
            scanAuthorization="Authorized",
        ),
        completed("21001"),
    ]
    for command, kind in (("21002", "scanStarted"), ("21003", "scanStopped")):
        domain = event(kind, command, scanSessionId="21004")
        setup += (
            [domain, completed(command)]
            if command == "21002"
            else [completed(command), domain]
        )
        if command == "21002":
            setup += [
                event(
                    "advertisementObserved",
                    scanSessionId="21004",
                    peripheralId="876",
                    advertisedServices=[ledger.SERVICE],
                )
            ]
    for command, connection, discovery, gatt in (("21005", "21007", "21008", "21009"),):
        setup += [completed(command)]
        setup += [
            event(
                "connectionStateChanged",
                command,
                connectionId=connection,
                connectionState=state,
            )
            for state in ("Connecting", "Connected")
        ]
        setup += [
            event(
                "gattDiscovered",
                discovery,
                connectionId=connection,
                gattSessionId=gatt,
                gattServices=ui_database("30"),
            ),
            completed(discovery),
        ]
    setup += [
        marker(
            "rf-watchdog:read-dispatched:activeCommandId=21010:queuedCommandId=21011:connectionId=21007:gattSessionId=21009:characteristicId=30"
        )
    ]
    timeout: dict[str, object] = {
        "category": "timeout",
        "message": "Apple GATT operation watchdog expired: read characteristic",
    }
    loss = event(
        "connectionStateChanged", connectionId="21007", connectionState="Disconnected"
    )
    if android:
        timeout = {
            "category": "timeout",
            "message": "read characteristic exceeded the native callback watchdog",
            "platformDetails": {
                "platform": "platform_android",
                "name": "AndroidBleBackend",
            },
        }
        loss = event(
            "connectionStateChanged",
            connectionId="21007",
            error={
                "category": "connection",
                "platformDetails": {
                    "platform": "platform_android",
                    "name": "BluetoothGatt",
                },
            },
        )
    # Android loss and queued terminal may precede the active terminal.
    gate = ([loss] if android else []) + [
        completed(
            "21011",
            {
                "category": "operation_aborted",
                "message": "native recovery retired pending operation",
            },
        ),
        completed("21010", timeout),
        marker(ledger.TIMEOUT),
        marker(ledger.ABORTED),
        marker(ledger.AWAITING),
    ]
    final = ([] if android else [loss]) + [
        marker(ledger.RETIRED),
        marker(ledger.SILENCE),
    ]
    final += [
        event("scanStarted", "21012", scanSessionId="21014"),
        completed("21012"),
        event(
            "advertisementObserved",
            scanSessionId="21014",
            peripheralId="876",
            advertisedServices=[ledger.SERVICE],
        ),
        completed("21013"),
        event("scanStopped", "21013", scanSessionId="21014"),
        completed("21020"),
    ]
    final += [
        event(
            "connectionStateChanged",
            "21020",
            connectionId="21021",
            connectionState=state,
        )
        for state in ("Connecting", "Connected")
    ]
    final += [
        event(
            "gattDiscovered",
            "21022",
            connectionId="21021",
            gattSessionId="21023",
            gattServices=ui_database("31"),
        ),
        completed("21022"),
    ]
    # Existing projection permits a terminal to arrive before the domain in JS.
    final += [
        completed("21024"),
        event(
            "characteristicRead",
            "21024",
            connectionId="21021",
            gattSessionId="21023",
            characteristicId="31",
            value=[0],
        ),
        marker(
            "rf-watchdog:recovery-read:passed:commandId=21024:connectionId=21021:gattSessionId=21023:characteristicId=31:value=00"
        ),
    ]
    final += [
        event(
            "connectionStateChanged",
            "21025",
            connectionId="21021",
            connectionState=state,
        )
        for state in ("Disconnecting", "Disconnected")
    ]
    final += [completed("21025"), marker(ledger.CLOSE), marker(ledger.PASSED)]
    return "\n".join(setup) + "\n", "\n".join(gate) + "\n", "\n".join(final) + "\n"
