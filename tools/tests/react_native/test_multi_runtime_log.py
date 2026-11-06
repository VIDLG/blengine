from __future__ import annotations

import re
import unittest
from pathlib import Path

from tools.react_native.check_runtime_log import (
    check_rf_multi,
    check_rf_multi_fault,
    check_rf_multi_soak,
)

SERVICE_A = "fd000000-0000-4000-8000-000000000000"
SERVICE_B = "fd200000-0000-4000-8000-000000000000"
LOG_PATH = Path("multi-runtime.log")


def marker_lines() -> list[str]:
    return [
        "rf-multi:listener:registered-before-initialize",
        "rf-multi:step:initialize:passed:commandId=9200",
        f"rf-multi:step:scan-start:passed:commandId=9201:scanSessionId=9202:serviceUuids={SERVICE_A},{SERVICE_B}",
        f"rf-multi:step:advertisement:passed:index=0:serviceUuid={SERVICE_A}:peripheralId=42:localName=BleHubNrf52840:payloadAdvertisedServices={SERVICE_A}",
        f"rf-multi:step:advertisement:passed:index=1:serviceUuid={SERVICE_B}:peripheralId=43:localName=BleHubNrf52840B:payloadAdvertisedServices={SERVICE_B}",
        "rf-multi:step:scan-stop:passed:commandId=9203:scanSessionId=9202:requested=true",
        "rf-multi:step:silence:passed:scanSessionId=9202:observationMs=2000",
        f"rf-multi:step:connected:passed:index=0:commandId=9210:connectionId=9220:peripheralId=42:serviceUuid={SERVICE_A}",
        f"rf-multi:step:connected:passed:index=1:commandId=9220:connectionId=9230:peripheralId=43:serviceUuid={SERVICE_B}",
        f"rf-multi:step:gatt-discovery:passed:index=0:commandId=9230:connectionId=9220:gattSessionId=9240:peripheralId=42:serviceUuid={SERVICE_A}:serviceCount=1:characteristicCount=5:descriptorCount=2",
        f"rf-multi:step:gatt-discovery:passed:index=1:commandId=9240:connectionId=9230:gattSessionId=9250:peripheralId=43:serviceUuid={SERVICE_B}:serviceCount=1:characteristicCount=5:descriptorCount=2",
        f"rf-multi:step:io:passed:index=0:operation=read-initial:commandId=9300:connectionId=9220:gattSessionId=9240:characteristicId=100:peripheralId=42:serviceUuid={SERVICE_A}:value=00",
        f"rf-multi:step:io:passed:index=1:operation=read-initial:commandId=9320:connectionId=9230:gattSessionId=9250:characteristicId=200:peripheralId=43:serviceUuid={SERVICE_B}:value=00",
        f"rf-multi:step:io:passed:index=0:operation=write-response:commandId=9301:connectionId=9220:gattSessionId=9240:characteristicId=100:peripheralId=42:serviceUuid={SERVICE_A}:value=10",
        f"rf-multi:step:io:passed:index=1:operation=write-response:commandId=9321:connectionId=9230:gattSessionId=9250:characteristicId=200:peripheralId=43:serviceUuid={SERVICE_B}:value=20",
        f"rf-multi:step:io:passed:index=0:operation=readback:commandId=9302:connectionId=9220:gattSessionId=9240:characteristicId=100:peripheralId=42:serviceUuid={SERVICE_A}:value=10",
        f"rf-multi:step:io:passed:index=1:operation=readback:commandId=9322:connectionId=9230:gattSessionId=9250:characteristicId=200:peripheralId=43:serviceUuid={SERVICE_B}:value=20",
        f"rf-multi:step:rssi:passed:index=0:commandId=9270:connectionId=9220:peripheralId=42:serviceUuid={SERVICE_A}:rssiDbm=-57",
        f"rf-multi:step:rssi:passed:index=1:commandId=9280:connectionId=9230:peripheralId=43:serviceUuid={SERVICE_B}:rssiDbm=-61",
        f"rf-multi:step:notification-enable:passed:index=0:commandId=9560:connectionId=9220:gattSessionId=9240:subscriptionId=9561:characteristicId=300:peripheralId=42:serviceUuid={SERVICE_A}:actualMode=Notification",
        f"rf-multi:step:notification-enable:passed:index=1:commandId=9570:connectionId=9230:gattSessionId=9250:subscriptionId=9571:characteristicId=400:peripheralId=43:serviceUuid={SERVICE_B}:actualMode=Notification",
        f"rf-multi:step:notification-value:passed:index=0:connectionId=9220:gattSessionId=9240:subscriptionId=9561:characteristicId=300:peripheralId=42:serviceUuid={SERVICE_A}:value=ef",
        f"rf-multi:step:notification-value:passed:index=1:connectionId=9230:gattSessionId=9250:subscriptionId=9571:characteristicId=400:peripheralId=43:serviceUuid={SERVICE_B}:value=ef",
        f"rf-multi:step:notification-disable:passed:index=0:commandId=9580:connectionId=9220:gattSessionId=9240:subscriptionId=9561:characteristicId=300:peripheralId=42:serviceUuid={SERVICE_A}:postDisableSilenceMs=2000",
        f"rf-multi:step:notification-disable:passed:index=1:commandId=9590:connectionId=9230:gattSessionId=9250:subscriptionId=9571:characteristicId=400:peripheralId=43:serviceUuid={SERVICE_B}:postDisableSilenceMs=2000",
        f"rf-multi:step:disconnected:passed:index=0:commandId=9250:connectionId=9220:peripheralId=42:serviceUuid={SERVICE_A}",
        f"rf-multi:step:disconnected:passed:index=1:commandId=9260:connectionId=9230:peripheralId=43:serviceUuid={SERVICE_B}",
        "rf-multi:step:close:passed:listener-removed=true:probe-closed=true",
        "rf-multi:passed:peripheralCount=2:connectionCount=2:gattSessionCount=2:routeMode=both:subscriptionMode=notification",
    ]


def log(lines: list[str]) -> str:
    return "\n".join(f"BLEHUB_NITRO_RUNTIME {line}" for line in lines)


def soak_marker_lines(cycles: int) -> list[str]:
    lines: list[str] = []
    for cycle in range(cycles):
        offset = cycle * 1000
        lines.append(f"rf-multi-soak:cycle:{cycle + 1}:started")
        for marker in marker_lines():
            shifted = re.sub(
                r"(commandId|subscriptionId)=(\d+)",
                lambda match, offset=offset: (
                    f"{match.group(1)}={int(match.group(2)) + offset}"
                ),
                marker,
            )
            lines.append(shifted)
        lines.append(f"rf-multi-soak:cycle:{cycle + 1}:passed")
    lines.extend(
        [
            "rf-multi-soak:close:passed:probe-closed=true",
            f"rf-multi-soak:passed:cycles={cycles}:routeMode=both:subscriptionMode=notification",
        ]
    )
    return lines


def fault_marker_lines() -> list[str]:
    return [
        "rf-multi-fault:listener:registered-before-initialize",
        "rf-multi-fault:ready-for-a-fault:connectionA=101:connectionB=102:gattB=202",
        "rf-multi-fault:a-passive-disconnected:connectionId=101:error=none",
        "rf-multi-fault:b-survived-a-fault:connectionId=102:gattSessionId=202:value=00",
        "rf-multi-fault:awaiting-a-restart",
        "rf-multi-fault:a-reconnected:connectionId=103:gattSessionId=203:value=00",
        "rf-multi-fault:close:passed:listener-removed=true:probe-closed=true",
        "rf-multi-fault:passed:passiveFault=dk_a:bSurvived=true:aReconnected=true",
    ]


class MultiRuntimeLogTests(unittest.TestCase):
    def test_accepts_two_distinct_correlated_devices(self) -> None:
        check_rf_multi(log(marker_lines()), f"{SERVICE_A},{SERVICE_B}", LOG_PATH)

    def test_accepts_one_probe_multi_device_soak(self) -> None:
        check_rf_multi_soak(
            log(soak_marker_lines(2)),
            f"{SERVICE_A},{SERVICE_B}",
            LOG_PATH,
            cycles=2,
        )

    def test_accepts_two_indication_routes(self) -> None:
        lines = [
            line.replace("notification-enable", "indication-enable")
            .replace("notification-value", "indication-value")
            .replace("notification-disable", "indication-disable")
            .replace("actualMode=Notification", "actualMode=Indication")
            for line in marker_lines()
        ]
        lines[-1] = lines[-1].replace(
            "subscriptionMode=notification", "subscriptionMode=indication"
        )
        check_rf_multi(
            log(lines),
            f"{SERVICE_A},{SERVICE_B}",
            LOG_PATH,
            subscription_mode="indication",
        )

    def test_accepts_single_notification_route_diagnostic_mode(self) -> None:
        lines = [
            line
            for line in marker_lines()
            if not ("notification-" in line and f"serviceUuid={SERVICE_B}" in line)
        ]
        check_rf_multi(
            log(lines),
            f"{SERVICE_A},{SERVICE_B}",
            LOG_PATH,
            notification_mode="only-dk-a",
        )

    def test_rejects_cross_device_gatt_connection(self) -> None:
        lines = marker_lines()
        lines[9] = lines[9].replace("connectionId=9220", "connectionId=9230")
        with self.assertRaises(AssertionError):
            check_rf_multi(log(lines), f"{SERVICE_A},{SERVICE_B}", LOG_PATH)

    def test_rejects_duplicate_peripheral_identity(self) -> None:
        lines = marker_lines()
        lines[4] = lines[4].replace("peripheralId=43", "peripheralId=42")
        with self.assertRaises(AssertionError):
            check_rf_multi(log(lines), f"{SERVICE_A},{SERVICE_B}", LOG_PATH)

    def test_accepts_fault_peer_survival_and_reconnect(self) -> None:
        check_rf_multi_fault(
            log(fault_marker_lines()), f"{SERVICE_A},{SERVICE_B}", LOG_PATH
        )

    def test_rejects_fault_reconnect_before_peer_survival(self) -> None:
        lines = fault_marker_lines()
        lines[3], lines[5] = lines[5], lines[3]
        with self.assertRaises(AssertionError):
            check_rf_multi_fault(log(lines), f"{SERVICE_A},{SERVICE_B}", LOG_PATH)

    def test_rejects_unclassified_passive_disconnect(self) -> None:
        lines = fault_marker_lines()
        lines[2] = lines[2].replace("error=none", "error=gatt")
        with self.assertRaises(AssertionError):
            check_rf_multi_fault(log(lines), f"{SERVICE_A},{SERVICE_B}", LOG_PATH)


if __name__ == "__main__":
    unittest.main()
