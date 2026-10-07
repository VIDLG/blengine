"""Protect the RN RF scan ledger from false-positive host evidence."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.react_native.check_runtime_log import (
    check_rf_recovery,
    check_rf_reload,
    check_rf_scan,
    check_runtime_reload,
)

SERVICE = "fd000000-0000-4000-8000-000000000000"
LOG_PATH = Path("device-rf-scan.log")
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


CONNECT_MARKERS = [
    *MARKERS[:-2],
    f"rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connection-silence:passed:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    MARKERS[-2],
    f"rf-connect:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:disconnectCommandId=9006:scanSessionId=9004",
]

RSSI_MARKERS = [
    *CONNECT_MARKERS[:8],
    f"rf:step:rssi:passed:commandId=9026:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:rssiDbm=-65",
    *CONNECT_MARKERS[8:-1],
    f"rf-rssi:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:rssiCommandId=9026:rssiDbm=-65:disconnectCommandId=9006:scanSessionId=9004",
]

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

NOTIFICATION_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:notification-enable:passed:commandId=9015:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:actualMode=Notification",
    "rf:step:notification-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:value=ef",
    "rf:step:notification-disable:passed:commandId=9016:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:state=Disabled:postDisableSilenceMs=2000",
    *GATT_MARKERS[9:-1],
    f"rf-notification:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:enableCommandId=9015:disableCommandId=9016:subscriptionId=9017:notificationValue=ef:postDisableSilenceMs=2000:disconnectCommandId=9006:scanSessionId=9004",
]

INDICATION_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:indication-rejection:passed:commandId=9018:connectionId=9007:gattSessionId=9009:subscriptionId=9022:characteristicId=fff20000:error=not_supported",
    "rf:step:indication-enable:passed:commandId=9019:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=fff50000:actualMode=Indication",
    "rf:step:indication-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=fff50000:value=ef",
    "rf:step:indication-disable:passed:commandId=9020:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=fff50000:state=Disabled:postDisableSilenceMs=2000",
    *GATT_MARKERS[9:-1],
    f"rf-indication:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:rejectionCommandId=9018:enableCommandId=9019:disableCommandId=9020:subscriptionId=9021:indicationValue=ef:postDisableSilenceMs=2000:disconnectCommandId=9006:scanSessionId=9004",
]

PASSIVE_DISCONNECT_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:notification-enable:passed:commandId=9015:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:actualMode=Notification",
    "rf:step:notification-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:value=ef",
    f"rf:step:disconnected:passed:commandId=none:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connection-silence:passed:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    f"rf:step:recovery-connecting:passed:commandId=9023:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:recovery-connected:passed:commandId=9023:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:recovery-disconnected:passed:commandId=9025:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:recovery-connection-silence:passed:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    GATT_MARKERS[-2],
    f"rf-passive-disconnect:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:subscriptionId=9017:recoveryConnectCommandId=9023:recoveryConnectionId=9024:recoveryDisconnectCommandId=9025:notificationValue=ef:passiveDisconnectCommandId=none:scanSessionId=9004",
]


def recovery_markers(cycles: int = 3) -> list[str]:
    prefix = "rf-recovery-ten" if cycles == 10 else "rf-recovery"
    markers = [
        f"{prefix}:listener:registered-before-initialize",
        f"{prefix}:initialize:passed:commandId=10000",
    ]
    for cycle in range(cycles):
        base_id = 10_000 + cycle * 100

        def id_(offset: int, base: int = base_id) -> int:
            return base + offset

        cycle_prefix = f"{prefix}:cycle:{cycle}:"
        markers.extend(
            (
                cycle_prefix + "start",
                f"{cycle_prefix}scan:passed:scanSessionId={id_(1)}:startCommandId={id_(2)}:stopCommandId={id_(3)}:peripheralId=42:serviceUuid={SERVICE}:silenceMs=2000",
                f"{cycle_prefix}connected:passed:commandId={id_(5)}:connectionId={id_(4)}:peripheralId=42",
                f"{cycle_prefix}gatt:passed:commandId={id_(7)}:connectionId={id_(4)}:gattSessionId={id_(6)}:serviceCount=1:characteristicCount=5:descriptorCount=2",
                f"{cycle_prefix}notification-enable:passed:commandId={id_(9)}:connectionId={id_(4)}:gattSessionId={id_(6)}:subscriptionId={id_(8)}:characteristicId=51:actualMode=Notification",
                f"{cycle_prefix}notification-value:passed:connectionId={id_(4)}:gattSessionId={id_(6)}:subscriptionId={id_(8)}:value=ef",
                f"{cycle_prefix}passive-disconnect:passed:commandId=none:connectionId={id_(4)}:error=connection",
                f"{cycle_prefix}old-route-silence:passed:connectionId={id_(4)}:gattSessionId={id_(6)}:subscriptionId={id_(8)}:observationMs=1000",
                f"{cycle_prefix}recovery-connected:passed:commandId={id_(11)}:connectionId={id_(10)}:peripheralId=42",
                f"{cycle_prefix}recovery-disconnected:passed:commandId={id_(12)}:connectionId={id_(10)}:observationMs=1000",
                f"{cycle_prefix}cycle:passed:scanSessionId={id_(1)}:connectionId={id_(4)}:recoveryConnectionId={id_(10)}",
            )
        )
    markers.extend(
        (
            f"{prefix}:close:passed:listener-removed=true:probe-closed=true",
            f"{prefix}:passed:cycles={cycles}",
        )
    )
    return markers


def reload_markers() -> list[str]:
    def generation() -> list[str]:
        return [
            "rf-reload:listener:registered-before-initialize",
            "rf-reload:initialize:passed:commandId=11000",
            f"rf-reload:scan:passed:serviceUuid={SERVICE}:peripheralId=42:scanSessionId=11001",
            "rf-reload:connected:passed:commandId=11005:connectionId=11004:peripheralId=42",
            "rf-reload:gatt:passed:commandId=11007:connectionId=11004:gattSessionId=11006",
            "rf-reload:notification-enable:passed:commandId=11009:connectionId=11004:gattSessionId=11006:subscriptionId=11008:characteristicId=14:actualMode=Notification",
            "rf-reload:notification-value:passed:connectionId=11004:gattSessionId=11006:subscriptionId=11008:value=ef",
            "rf-reload:active:peripheralId=42:connectionId=11004:subscriptionId=11008",
        ]

    return [
        *generation(),
        "rf-reload:native-triggered",
        "rf-reload:cleanup-started",
        "rf-reload:fixture-host-disconnected",
        *generation(),
        "rf-reload:host-complete:passed",
    ]


def normal_reload_markers() -> list[str]:
    generation = [
        "reload:start",
        "reload:round-trip:passed",
        "reload:dispatch:accepted",
        "reload:completion:18446744073709551615:ok",
        "reload:ready",
    ]
    return [*generation, "reload:cleanup-started", *generation]


class RuntimeReloadLogTests(unittest.TestCase):
    def test_accepts_current_two_generation_protocol(self) -> None:
        check_runtime_reload(log(normal_reload_markers()), LOG_PATH)

    def test_requires_cleanup_and_both_generations(self) -> None:
        markers = normal_reload_markers()
        for invalid in (markers[:5] + markers[6:], markers[:-1]):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_runtime_reload(log(invalid), LOG_PATH)


class RfReloadLogTests(unittest.TestCase):
    def test_rejects_missing_teardown_wrong_order_and_wrong_rf_correlation(
        self,
    ) -> None:
        markers = reload_markers()
        check_rf_reload(log(markers), SERVICE, LOG_PATH)
        for invalid in (
            markers[:9] + markers[10:],  # missing cleanup
            markers[:9]
            + [markers[10], markers[9]]
            + markers[11:],  # fixture before cleanup
            markers[:10]
            + markers[11:19]
            + [markers[10]]
            + markers[19:],  # fixture after gen-2 active
            markers[:14]
            + [markers[14].replace("connectionId=11004", "connectionId=10004")]
            + markers[15:],
            markers[:6] + [markers[6].replace("value=ef", "value=00")] + markers[7:],
            markers[:2] + [markers[2].replace(SERVICE, "wrong-service")] + markers[3:],
            markers[:10] + ["rf-reload:fixture-host-disconnected"] + markers[10:],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_reload(log(invalid), SERVICE, LOG_PATH)


class RfRecoveryLogTests(unittest.TestCase):
    def test_requires_ten_complete_ordered_cycles(self) -> None:
        markers = recovery_markers(cycles=10)
        check_rf_recovery(log(markers), SERVICE, LOG_PATH, cycles=10)
        check_rf_recovery(log(markers[:-1]), SERVICE, LOG_PATH, cycles=10)
        for invalid in (
            markers[:-2] + markers[-1:],  # missing close even if JS summary appeared
            markers[:101] + markers[102:],  # missing ninth-cycle phase
            markers[:2] + markers[13:],  # cycle 0 missing
            markers[:-1] + ["rf-recovery-ten:passed:cycles=9"],
            markers[:101]
            + [markers[101].replace("cycle:9", "cycle:8")]
            + markers[102:],
            markers[:106]
            + [markers[106].replace("subscriptionId=10908", "subscriptionId=10808")]
            + markers[107:],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_recovery(log(invalid), SERVICE, LOG_PATH, cycles=10)

    def test_requires_exact_ordered_three_cycle_ledger(self) -> None:
        markers = recovery_markers()
        check_rf_recovery(log(markers), SERVICE, LOG_PATH)
        check_rf_recovery(log(markers[:-1]), SERVICE, LOG_PATH)
        for invalid in (
            markers[:13] + markers[14:],  # missing second-cycle start
            markers[:18] + [markers[19], markers[18]] + markers[20:],
            markers[:13] + [markers[13].replace("cycle:1", "cycle:0")] + markers[14:],
            markers[:16]
            + [markers[16].replace("connectionId=10104", "connectionId=10004")]
            + markers[17:],
            markers[:8]
            + [markers[8].replace("commandId=none", "commandId=10012")]
            + markers[9:],
            markers[:9]
            + [markers[9].replace("observationMs=1000", "observationMs=0")]
            + markers[10:],
            markers[:-1] + ["rf-recovery:passed:cycles=2"],
            markers[:1] + markers[:1] + markers[1:],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_recovery(log(invalid), SERVICE, LOG_PATH)


class RfScanLogTests(unittest.TestCase):
    def test_accepts_canonical_u64_peripheral_ids(self) -> None:
        for peripheral_id in ("0", "18446744073709551615"):
            with self.subTest(peripheral_id=peripheral_id):
                markers = [
                    marker.replace("peripheralId=42", f"peripheralId={peripheral_id}")
                    for marker in MARKERS
                ]
                check_rf_scan(log(markers), SERVICE, LOG_PATH)

    def test_rejects_invalid_peripheral_ids(self) -> None:
        for peripheral_id in ("00", "042", "-1", "18446744073709551616"):
            with (
                self.subTest(peripheral_id=peripheral_id),
                self.assertRaises(AssertionError),
            ):
                markers = [
                    marker.replace("peripheralId=42", f"peripheralId={peripheral_id}")
                    for marker in MARKERS
                ]
                check_rf_scan(log(markers), SERVICE, LOG_PATH)

    def test_connect_requires_ordered_correlated_domain_and_terminal_evidence(
        self,
    ) -> None:
        check_rf_scan(log(CONNECT_MARKERS), SERVICE, LOG_PATH, connect=True)
        for invalid in (
            [
                *CONNECT_MARKERS[:7],
                CONNECT_MARKERS[8],
                CONNECT_MARKERS[7],
                *CONNECT_MARKERS[9:],
            ],
            [*CONNECT_MARKERS[:8], CONNECT_MARKERS[7], *CONNECT_MARKERS[8:]],
            [
                *CONNECT_MARKERS[:-1],
                CONNECT_MARKERS[-1].replace("connectionId=9007", "connectionId=9008"),
            ],
            [
                *CONNECT_MARKERS[:6],
                CONNECT_MARKERS[6].replace("peripheralId=42", "peripheralId=0"),
                *CONNECT_MARKERS[7:],
            ],
            [
                *CONNECT_MARKERS[:9],
                CONNECT_MARKERS[9].replace("observationMs=1000", "observationMs=0"),
                *CONNECT_MARKERS[10:],
            ],
            [*CONNECT_MARKERS[:-1], MARKERS[-1]],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(log(invalid), SERVICE, LOG_PATH, connect=True)

    def test_connected_rssi_requires_ordered_correlated_int16_and_close(self) -> None:
        check_rf_scan(log(RSSI_MARKERS), SERVICE, LOG_PATH, connect=True, rssi=True)
        for invalid in (
            RSSI_MARKERS[:8] + RSSI_MARKERS[9:],
            RSSI_MARKERS[:8] + [RSSI_MARKERS[9], RSSI_MARKERS[8]] + RSSI_MARKERS[10:],
            RSSI_MARKERS[:8]
            + [RSSI_MARKERS[8].replace("commandId=9026", "commandId=9005")]
            + RSSI_MARKERS[9:],
            RSSI_MARKERS[:8]
            + [RSSI_MARKERS[8].replace("connectionId=9007", "connectionId=9999")]
            + RSSI_MARKERS[9:],
            RSSI_MARKERS[:8]
            + [RSSI_MARKERS[8].replace("rssiDbm=-65", "rssiDbm=32768")]
            + RSSI_MARKERS[9:],
            RSSI_MARKERS[:8]
            + [RSSI_MARKERS[8].replace("rssiDbm=-65", "rssiDbm=-65.5")]
            + RSSI_MARKERS[9:],
            RSSI_MARKERS[:-1]
            + [RSSI_MARKERS[-1].replace("rssiCommandId=9026", "rssiCommandId=9008")],
            RSSI_MARKERS[:-1]
            + [RSSI_MARKERS[-1].replace("rssiDbm=-65", "rssiDbm=-60")],
            RSSI_MARKERS[:-1]
            + [RSSI_MARKERS[-1].replace("rssiDbm=-65", "rssiDbm=-650")],
            RSSI_MARKERS[:8] + [RSSI_MARKERS[8]] + RSSI_MARKERS[8:],
            RSSI_MARKERS[:-2] + RSSI_MARKERS[-1:],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(log(invalid), SERVICE, LOG_PATH, connect=True, rssi=True)

    def test_gatt_requires_database_and_connection_order(self) -> None:
        check_rf_scan(log(GATT_MARKERS), SERVICE, LOG_PATH, connect=True, gatt=True)
        for invalid in (
            [*GATT_MARKERS[:8], GATT_MARKERS[9], GATT_MARKERS[8], *GATT_MARKERS[10:]],
            [
                *GATT_MARKERS[:8],
                GATT_MARKERS[8].replace("descriptorCount=2", "descriptorCount=1"),
                *GATT_MARKERS[9:],
            ],
            [
                *GATT_MARKERS[:-1],
                GATT_MARKERS[-1].replace("gattCommandId=9008", "gattCommandId=9009"),
            ],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(log(invalid), SERVICE, LOG_PATH, connect=True, gatt=True)

    def test_gatt_io_requires_read_write_evidence_and_local_no_response_terminal(
        self,
    ) -> None:
        check_rf_scan(
            log(IO_MARKERS), SERVICE, LOG_PATH, connect=True, gatt=True, io=True
        )
        invalid_logs = (
            [
                *IO_MARKERS[:12],
                IO_MARKERS[13],
                IO_MARKERS[12],
                *IO_MARKERS[14:],
            ],
            [
                *IO_MARKERS[:12],
                IO_MARKERS[12].replace(
                    "terminal=local-submission", "terminal=remote-success"
                ),
                *IO_MARKERS[13:],
            ],
            [
                *IO_MARKERS[:-1],
                IO_MARKERS[-1].replace("readCommandId=9010", "readCommandId=9011"),
            ],
        )
        for invalid in invalid_logs:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(
                    log(invalid), SERVICE, LOG_PATH, connect=True, gatt=True, io=True
                )

    def test_notification_requires_enable_value_disable_and_stale_silence(self) -> None:
        check_rf_scan(
            log(NOTIFICATION_MARKERS),
            SERVICE,
            LOG_PATH,
            connect=True,
            gatt=True,
            notification=True,
        )
        invalid_logs = (
            [
                *NOTIFICATION_MARKERS[:11],
                NOTIFICATION_MARKERS[12],
                NOTIFICATION_MARKERS[11],
                *NOTIFICATION_MARKERS[13:],
            ],
            [
                *NOTIFICATION_MARKERS[:-1],
                NOTIFICATION_MARKERS[-1].replace(
                    "notificationValue=ef", "notificationValue=00"
                ),
            ],
            [
                *NOTIFICATION_MARKERS[:11],
                NOTIFICATION_MARKERS[11].replace(
                    "postDisableSilenceMs=2000", "postDisableSilenceMs=0"
                ),
                *NOTIFICATION_MARKERS[12:],
            ],
        )
        for invalid in invalid_logs:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(
                    log(invalid),
                    SERVICE,
                    LOG_PATH,
                    connect=True,
                    gatt=True,
                    notification=True,
                )

    def test_passive_disconnect_requires_stale_silence_and_recovery(self) -> None:
        check_rf_scan(
            log(PASSIVE_DISCONNECT_MARKERS),
            SERVICE,
            LOG_PATH,
            connect=True,
            gatt=True,
            passive_disconnect=True,
        )
        invalid_logs = (
            [
                *PASSIVE_DISCONNECT_MARKERS[:12],
                PASSIVE_DISCONNECT_MARKERS[13],
                PASSIVE_DISCONNECT_MARKERS[12],
                *PASSIVE_DISCONNECT_MARKERS[14:],
            ],
            [
                *PASSIVE_DISCONNECT_MARKERS[:-1],
                PASSIVE_DISCONNECT_MARKERS[-1].replace(
                    "recoveryConnectionId=9024", "recoveryConnectionId=9025"
                ),
            ],
            [
                *PASSIVE_DISCONNECT_MARKERS[:-1],
                PASSIVE_DISCONNECT_MARKERS[-1].replace(
                    "passiveDisconnectCommandId=none", "passiveDisconnectCommandId=9006"
                ),
            ],
        )
        for invalid in invalid_logs:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(
                    log(invalid),
                    SERVICE,
                    LOG_PATH,
                    connect=True,
                    gatt=True,
                    passive_disconnect=True,
                )

    def test_indication_requires_strict_rejection_and_pure_indication_flow(
        self,
    ) -> None:
        check_rf_scan(
            log(INDICATION_MARKERS),
            SERVICE,
            LOG_PATH,
            connect=True,
            gatt=True,
            indication=True,
        )
        invalid_logs = (
            [
                *INDICATION_MARKERS[:9],
                INDICATION_MARKERS[10],
                INDICATION_MARKERS[9],
                *INDICATION_MARKERS[11:],
            ],
            [
                *INDICATION_MARKERS[:-1],
                INDICATION_MARKERS[-1].replace(
                    "rejectionCommandId=9018", "rejectionCommandId=9019"
                ),
            ],
            [
                *INDICATION_MARKERS[:9],
                INDICATION_MARKERS[9].replace(
                    "subscriptionId=9022", "subscriptionId=9021"
                ),
                *INDICATION_MARKERS[10:],
            ],
            [
                *INDICATION_MARKERS[:12],
                INDICATION_MARKERS[12].replace(
                    "postDisableSilenceMs=2000", "postDisableSilenceMs=0"
                ),
                *INDICATION_MARKERS[13:],
            ],
            [
                *INDICATION_MARKERS[:-1],
                INDICATION_MARKERS[-1].replace(
                    "indicationValue=ef", "indicationValue=00"
                ),
            ],
        )
        for invalid in invalid_logs:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(
                    log(invalid),
                    SERVICE,
                    LOG_PATH,
                    connect=True,
                    gatt=True,
                    indication=True,
                )

    def test_requires_ordered_unique_events_from_one_peripheral(self) -> None:
        check_rf_scan(log(MARKERS), SERVICE, LOG_PATH)
        for invalid in (
            [*MARKERS[:2], MARKERS[1], *MARKERS[2:]],
            [MARKERS[1], MARKERS[0], *MARKERS[2:]],
            [*MARKERS[:3], *MARKERS[4:]],
            [
                *MARKERS[:4],
                MARKERS[4].replace("requested=true", "requested=false"),
                *MARKERS[5:],
            ],
            [*MARKERS[:-1], MARKERS[-1].replace("peripheralId=42", "peripheralId=43")],
            [
                *MARKERS[:-1],
                MARKERS[-1].replace(
                    "payloadContainsFilterUuid=false", "payloadContainsFilterUuid=true"
                ),
            ],
            [
                *MARKERS[:-1],
                MARKERS[-1].replace(SERVICE, "0000180f-0000-1000-8000-00805f9b34fb"),
            ],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_scan(log(invalid), SERVICE, LOG_PATH)


if __name__ == "__main__":
    _ = unittest.main()
