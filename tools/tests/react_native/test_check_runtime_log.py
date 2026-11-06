"""Protect the RN RF scan ledger from false-positive host evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tests.support.rf_lab.markers import (
    CANCELLATION_MARKERS,
    GATT_MARKERS,
    IO_MARKERS,
    MARKERS,
    SERVICE,
    controlled_markers,
    log,
)
from tools.react_native.check_runtime_log import (
    check_rf_cancellation,
    check_rf_lab_suite,
    check_rf_recovery,
    check_rf_reload,
    check_rf_scan,
    check_runtime_reload,
)
from tools.tests.react_native.support.runtime_markers import (
    CONNECT_MARKERS,
    INDICATION_MARKERS,
    NOTIFICATION_MARKERS,
    PASSIVE_DISCONNECT_MARKERS,
    RSSI_MARKERS,
)

LOG_PATH = Path("device-rf-scan.log")


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


class RfLabSuiteLogTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.attempt = fake_attempt(Path(temporary.name), SERVICE)
        self.suite_log = Path(temporary.name) / "suite.log"

    def check(self, text: str, service: str, path: Path, **kwargs: object) -> None:
        check_rf_lab_suite(
            text,
            service,
            self.suite_log,
            cancellation_evidence=self.attempt.path,
            **kwargs,
        )

    def test_accepts_ten_ordered_flows_and_rejects_normal9(self) -> None:
        flow_markers = (
            MARKERS,
            CONNECT_MARKERS,
            GATT_MARKERS,
            IO_MARKERS,
            RSSI_MARKERS,
            NOTIFICATION_MARKERS,
            INDICATION_MARKERS,
            PASSIVE_DISCONNECT_MARKERS,
            controlled_markers(CANCELLATION_MARKERS, self.attempt.control),
        )
        modes = (
            "rf",
            "rf-connect",
            "rf-gatt",
            "rf-io",
            "rf-rssi",
            "rf-notification",
            "rf-indication",
            "rf-passive-disconnect",
            "rf-cancellation",
        )
        lines: list[str] = []
        for index, (mode, markers) in enumerate(zip(modes, flow_markers, strict=True)):
            lines.append(f"rf-lab-suite:flow-started:index={index}:mode={mode}")
            lines.extend(markers)
            lines.append(f"rf-lab-suite:flow-passed:index={index}:mode={mode}")
        lines.append("rf-lab-suite:passed:flows=9:passedCount=9:failureCount=0")
        with self.assertRaisesRegex(AssertionError, "coverage"):
            self.check(log(lines), SERVICE, LOG_PATH)
        from tests.peripherals.nrf52840.support.watchdog_reports import fake_watchdog

        watchdog = fake_watchdog(self.attempt)
        lines.pop()
        lines.insert(
            0,
            f"rf-lab-suite:runtime:session={self.attempt.control.session}:nonce=test-runtime:phase=start",
        )
        lines.append("rf-lab-suite:flow-started:index=9:mode=rf-watchdog")
        lines.extend(
            line.removeprefix("BLEHUB_NITRO_RUNTIME ")
            for line in watchdog.text.splitlines()
        )
        lines.extend(
            [
                "rf-lab-suite:flow-passed:index=9:mode=rf-watchdog",
                f"rf-lab-suite:runtime:session={self.attempt.control.session}:nonce=test-runtime:phase=close",
                "rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0",
            ]
        )
        from dataclasses import asdict

        from tests.peripherals.nrf52840.cancellation_control import (
            load_suite_controls,
            write_document,
        )
        from tools.tests.react_native.support.ios_receipts import (
            DEVICE_IDENTIFIER,
            host_marker,
            ios_bootstrap,
            ios_process_document,
        )

        controls = load_suite_controls(self.attempt.root)
        boot = ios_bootstrap(controls[0])
        lines.insert(
            0, host_marker("bootstrap", boot).removeprefix("BLEHUB_NITRO_RUNTIME ")
        )
        scoped = []
        for line in lines:
            scoped.append(line)
            for control in controls:
                for phase in control.phases:
                    if line == control.marker("observed", phase):
                        scoped.append(
                            host_marker(
                                "signal", {**boot, **asdict(control), "phase": phase}
                            ).removeprefix("BLEHUB_NITRO_RUNTIME ")
                        )
        lines = scoped
        keys = [f"{c.flow}:{p}" for c in controls for p in c.phases]
        write_document(
            self.suite_log.with_suffix(".rf-host-identity.json"),
            {
                "status": "passed",
                "failure": None,
                "controls": [asdict(c) for c in controls],
                "bootstrap": boot,
                "sent": keys,
                "native_acknowledged": keys,
                "js_acknowledged": keys,
                "device": "device",
                "device_identifier": DEVICE_IDENTIFIER,
            },
        )
        for boundary in ("bootstrap", "closed"):
            write_document(
                self.suite_log.with_suffix(f".rf-host-{boundary}-processes.json"),
                ios_process_document(),
            )
        self.check(log(lines), SERVICE, LOG_PATH)
        for invalid in (
            lines + [lines[-1]],
            [
                line.replace(
                    "nonce=test-runtime:phase=close", "nonce=new-runtime:phase=close"
                )
                for line in lines
            ],
            [line for line in lines if "mode=rf-watchdog" not in line],
        ):
            with self.assertRaises(AssertionError):
                self.check(log(invalid), SERVICE, LOG_PATH)
        name = "Custom RF: fixture"
        supplied: list[str] = []
        for line in lines:
            supplied.append(line.replace("localName=missing", f"localName={name}"))
            if line.startswith("rf-lab-suite:flow-started:"):
                supplied.append(
                    f"rf:start:serviceUuid={SERVICE}:expectedLocalName={name}:"
                    "initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004"
                )
        self.check(log(supplied), SERVICE, LOG_PATH, expected_local_name=name)
        # Check every flow, including cancellation's normalized GATT projection.
        for index, line in enumerate(supplied):
            if "rf:step:advertisement:passed:" not in line:
                continue
            invalid = supplied.copy()
            invalid[index] = line.replace(
                f"localName={name}", "localName=Foreign fixture"
            )
            with (
                self.subTest(index=index),
                self.assertRaisesRegex(AssertionError, "local name"),
            ):
                self.check(log(invalid), SERVICE, LOG_PATH, expected_local_name=name)
        with self.assertRaisesRegex(AssertionError, "launch fixture metadata"):
            self.check(log(lines), SERVICE, LOG_PATH, expected_local_name=name)
        self.check(
            log(
                [
                    line.replace(f"localName={name}", "localName=missing")
                    for line in supplied
                ]
            ),
            SERVICE,
            LOG_PATH,
            expected_local_name=name,
        )


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


class RfCancellationLogTests(unittest.TestCase):
    def test_accepts_exact_five_event_ledger(self) -> None:
        check_rf_cancellation(log(CANCELLATION_MARKERS), SERVICE, LOG_PATH)

    def test_allows_cancelled_terminal_to_interleave_with_active_events(self) -> None:
        interleaved_event = "rf-cancellation:event:kind=commandCompleted|characteristicRead|commandCompleted|characteristicRead|commandCompleted:commandId=9028|9027|9027|9029|9029:connectionId=none|9007|none|9007|none:gattSessionId=none|9009|none|9009|none:characteristicId=none|51|none|51|none:error=cancelled|ok|ok|ok|ok:value=none|00|none|10|none"
        markers = [
            interleaved_event if marker.startswith("rf-cancellation:event:") else marker
            for marker in CANCELLATION_MARKERS
        ]
        check_rf_cancellation(log(markers), SERVICE, LOG_PATH)

    def test_rejects_missing_duplicate_domain_leak_wrong_id_and_missing_recovery(
        self,
    ) -> None:
        invalid_logs = (
            CANCELLATION_MARKERS[:12] + CANCELLATION_MARKERS[13:],
            CANCELLATION_MARKERS + [CANCELLATION_MARKERS[9]],
            [
                marker.replace(
                    "connectionId=9007|none|none|9007|none",
                    "connectionId=9007|9007|none|9007|none",
                )
                if marker.startswith("rf-cancellation:event:")
                else marker
                for marker in CANCELLATION_MARKERS
            ],
            [
                marker.replace("targetCommandId=9028", "targetCommandId=9027")
                if marker.startswith("rf:step:cancellation-queued-accepted:")
                else marker
                for marker in CANCELLATION_MARKERS
            ],
            [
                marker
                for marker in CANCELLATION_MARKERS
                if not marker.startswith("rf-cancellation:event:")
            ],
        )
        for invalid in invalid_logs:
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                check_rf_cancellation(log(invalid), SERVICE, LOG_PATH)


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

    def test_io_requires_on_scan_and_close_boundary_fields(self) -> None:
        mutations = (
            (1, "adapterState=PoweredOn", "adapterState=PoweredOff"),
            (1, "scanAuthorization=Authorized", "scanAuthorization=Denied"),
            (1, "commandId=9001", "commandId=1"),
            (2, "startCommandId=9002", "startCommandId=1"),
            (2, "scanSessionId=9004", "scanSessionId=2"),
            (4, "stopCommandId=9003", "stopCommandId=1"),
            (4, "scanSessionId=9004", "scanSessionId=2"),
            (5, "scanSessionId=9004", "scanSessionId=2"),
            (-2, "probe-closed=true", "probe-closed=false"),
            (-2, "listener-removed=true", "listener-removed=false"),
        )
        for index, before, after in mutations:
            markers = list(IO_MARKERS)
            self.assertIn(before, markers[index])
            markers[index] = markers[index].replace(before, after)
            with (
                self.subTest(index=index, after=after),
                self.assertRaises(AssertionError),
            ):
                check_rf_scan(
                    log(markers), SERVICE, LOG_PATH, connect=True, gatt=True, io=True
                )

    def test_io_rejects_scalar_suffixes_key_prefixes_and_duplicate_fields(self) -> None:
        for index, marker in enumerate(IO_MARKERS):
            # Names belong to the launch/advertisement matcher, not scalar tokens.
            fields = [
                token
                for token in marker.split(":")
                if "=" in token and not token.startswith("localName=")
            ]
            for field in fields:
                key = field.split("=", 1)[0]
                mutations = (
                    *(
                        (
                            marker.replace(
                                field,
                                field
                                + ("junk" if key == "discoveredServiceCount" else "0"),
                            ),
                        )
                        if key != "payloadAdvertisedServices"
                        else ()
                    ),
                    marker.replace(field, "spoof" + field),
                    marker + ":" + field,
                    marker + ":" + key + "=foreign",
                )
                for mutation in mutations:
                    markers = list(IO_MARKERS)
                    markers[index] = mutation
                    with (
                        self.subTest(index=index, field=field, mutation=mutation),
                        self.assertRaises(AssertionError),
                    ):
                        check_rf_scan(
                            log(markers),
                            SERVICE,
                            LOG_PATH,
                            connect=True,
                            gatt=True,
                            io=True,
                        )

    def test_io_values_and_terminal_are_exact_and_all_routes_agree(self) -> None:
        for index, value in ((9, "00"), (10, "10"), (11, "10"), (12, "ab"), (13, "ab")):
            original = IO_MARKERS[index]
            for mutation in (
                original.replace("value=" + value, "value=" + value + "ff"),
                original.replace("characteristicId=fff10000", "characteristicId=999"),
                original.replace(":characteristicId=fff10000", ""),
                original.replace("characteristicId=fff10000", "characteristicId="),
                original.replace("characteristicId=fff10000", "characteristicId= "),
            ):
                markers = list(IO_MARKERS)
                markers[index] = mutation
                with (
                    self.subTest(index=index, mutation=mutation),
                    self.assertRaises(AssertionError),
                ):
                    check_rf_scan(
                        log(markers),
                        SERVICE,
                        LOG_PATH,
                        connect=True,
                        gatt=True,
                        io=True,
                    )
        for terminal in (
            "remote-delivery",
            "local-submission-extra",
            "local-submission=remote",
        ):
            markers = list(IO_MARKERS)
            markers[12] = markers[12].replace(
                "terminal=local-submission", "terminal=" + terminal
            )
            with self.subTest(terminal=terminal), self.assertRaises(AssertionError):
                check_rf_scan(
                    log(markers), SERVICE, LOG_PATH, connect=True, gatt=True, io=True
                )
        # Historical synthetic IDs remain opaque; this checks no actual native topology.
        for route in ("fff10000", "0", "51"):
            markers = [
                marker.replace("characteristicId=fff10000", "characteristicId=" + route)
                for marker in IO_MARKERS
            ]
            with self.subTest(shared_route=route):
                check_rf_scan(
                    log(markers), SERVICE, LOG_PATH, connect=True, gatt=True, io=True
                )

        markers = list(IO_MARKERS)
        markers[8] = markers[8].replace(
            "discoveredServiceCount=2", "discoveredServiceCount=20"
        )
        check_rf_scan(log(markers), SERVICE, LOG_PATH, connect=True, gatt=True, io=True)

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
