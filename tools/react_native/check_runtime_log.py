"""Assert the structured React Native Nitro runtime probe completed."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import cast

EXPECTED_BASIC_MARKERS = (
    "command-id-validation:passed",
    "round-trip:passed",
    "dispatch:accepted",
    "completion:18446744073709551615:ok",
    "listener-removal:passed",
    "close:resolved",
)
EXPECTED_STRESS_MARKERS = (
    "stress:start:8",
    "stress:cycle:0:passed",
    "stress:cycle:7:passed",
    "stress:recreate-close:passed",
    "stress:late-callback:passed",
    "stress:post-close-dispatch:passed",
    "stress:complete:passed",
)
EXPECTED_RELOAD_COUNTS = {
    "reload:start": 2,
    "reload:round-trip:passed": 2,
    "reload:dispatch:accepted": 2,
    "reload:completion:18446744073709551615:ok": 2,
    "reload:ready": 2,
    "reload:cleanup-started": 1,
}
FORBIDDEN_MARKERS = (":failed", "close:rejected:")
RF_PHASES = (
    "initialize",
    "scan-start",
    "advertisement",
    "scan-stop",
    "silence",
    "close",
)


def check_runtime_reload(log: str, log_path: Path) -> None:
    """Require exactly two JS generations and one first-generation cleanup."""
    wrong_counts = {
        marker: (log.count(marker), expected)
        for marker, expected in EXPECTED_RELOAD_COUNTS.items()
        if log.count(marker) != expected
    }
    if wrong_counts:
        raise AssertionError(
            f"wrong reload marker counts in {log_path}: {wrong_counts}"
        )


def check_rf_scan(
    log: str,
    service_uuid: str,
    log_path: Path,
    *,
    connect: bool = False,
    gatt: bool = False,
    io: bool = False,
    rssi: bool = False,
    notification: bool = False,
    indication: bool = False,
    passive_disconnect: bool = False,
) -> None:
    markers = [
        line.split("BLEHUB_NITRO_RUNTIME ", 1)[1].strip()
        for line in log.splitlines()
        if "BLEHUB_NITRO_RUNTIME rf:" in line
        or "BLEHUB_NITRO_RUNTIME rf-connect:" in line
        or "BLEHUB_NITRO_RUNTIME rf-gatt:" in line
        or "BLEHUB_NITRO_RUNTIME rf-io:" in line
        or "BLEHUB_NITRO_RUNTIME rf-rssi:" in line
        or "BLEHUB_NITRO_RUNTIME rf-notification:" in line
        or "BLEHUB_NITRO_RUNTIME rf-indication:" in line
        or "BLEHUB_NITRO_RUNTIME rf-passive-disconnect:" in line
    ]
    if passive_disconnect:
        phase_names = (
            *RF_PHASES[:-1],
            "connecting",
            "connected",
            "gatt-discovery",
            "notification-enable",
            "notification-value",
            "disconnected",
            "connection-silence",
            "recovery-connecting",
            "recovery-connected",
            "recovery-disconnected",
            "recovery-connection-silence",
            "close",
        )
    elif connect:
        phase_names = (
            *RF_PHASES[:-1],
            "connecting",
            "connected",
            *(("rssi",) if rssi else ()),
            *(
                (
                    "gatt-discovery",
                    "read-initial",
                    "write-response",
                    "read-response",
                    "write-without-response",
                    "read-without-response",
                )
                if io
                else (
                    "gatt-discovery",
                    "notification-enable",
                    "notification-value",
                    "notification-disable",
                )
                if notification
                else (
                    "gatt-discovery",
                    "indication-rejection",
                    "indication-enable",
                    "indication-value",
                    "indication-disable",
                )
                if indication
                else ("gatt-discovery",)
                if gatt
                else ()
            ),
            "disconnected",
            "connection-silence",
            "close",
        )
    else:
        phase_names = RF_PHASES
    phases = [f"rf:step:{phase}:passed:" for phase in phase_names]
    expected = [
        "rf:listener:registered-before-initialize",
        *phases,
        "rf-passive-disconnect:passed:"
        if passive_disconnect
        else "rf-indication:passed:"
        if indication
        else "rf-notification:passed:"
        if notification
        else "rf-io:passed:"
        if io
        else "rf-rssi:passed:"
        if rssi
        else "rf-gatt:passed:"
        if gatt
        else "rf-connect:passed:"
        if connect
        else "rf:passed:",
    ]
    positions = [
        [index for index, marker in enumerate(markers) if marker.startswith(prefix)]
        for prefix in expected
    ]
    if any(len(indices) != 1 for indices in positions):
        raise AssertionError(
            f"missing or duplicated RF markers in {log_path}: {positions}"
        )
    ordered = [indices[0] for indices in positions]
    if ordered != sorted(set(ordered)):
        raise AssertionError(f"out-of-order RF markers in {log_path}: {ordered}")
    advertisement = markers[ordered[3]]
    final = markers[ordered[-1]]
    match = re.search(r"peripheralId=([0-9]+)", advertisement)
    peripheral_id = match.group(1) if match else ""
    if (
        not re.fullmatch(r"0|[1-9][0-9]*", peripheral_id)
        or len(peripheral_id) > 20
        or (len(peripheral_id) == 20 and peripheral_id > "18446744073709551615")
    ):
        raise AssertionError(
            f"no canonical peripheral ID in RF advertisement: {log_path}"
        )
    payload = re.search(
        r"payloadAdvertisedServices=([^:]+):payloadContainsFilterUuid=(true|false)",
        advertisement,
    )
    if (
        payload is None
        or payload.group(2) != str(service_uuid in payload.group(1).split(",")).lower()
    ):
        raise AssertionError(
            f"RF payload evidence contradicts the advertised UUIDs: {log_path}"
        )
    advertisement_fields = (
        f"filterServiceUuid={service_uuid}",
        f"payloadAdvertisedServices={payload.group(1)}",
        f"payloadContainsFilterUuid={payload.group(2)}",
        "scanSessionId=9004",
        f"peripheralId={peripheral_id}",
    )
    if any(field not in advertisement for field in advertisement_fields):
        raise AssertionError(f"RF advertisement does not match the fixture: {log_path}")
    final_fields = (
        (
            f"serviceUuid={service_uuid}",
            "scanSessionId=9004",
            f"peripheralId={peripheral_id}",
        )
        if connect
        else advertisement_fields
    )
    if any(field not in final for field in final_fields):
        raise AssertionError(
            f"RF result does not match the observed peripheral: {log_path}"
        )
    if (
        "requested=true" not in markers[ordered[4]]
        or "observationMs=2000" not in markers[ordered[5]]
    ):
        raise AssertionError(
            f"missing requested stop or post-stop silence evidence: {log_path}"
        )
    if passive_disconnect:
        subscription_markers = (
            (
                markers[ordered[9]],
                "notification-enable",
                "9015",
                "actualMode=Notification",
            ),
            (markers[ordered[10]], "notification-value", None, "value=ef"),
        )
        for marker, state, command, value in subscription_markers:
            if not marker.startswith(f"rf:step:{state}:passed:") or any(
                field not in marker
                for field in (
                    *((f"commandId={command}",) if command else ()),
                    "connectionId=9007",
                    "gattSessionId=9009",
                    "subscriptionId=9017",
                    value,
                )
            ):
                raise AssertionError(
                    f"RF passive-disconnect subscription evidence is incomplete: {log_path}"
                )
        phase_fields = (
            (11, "disconnected", ("commandId=none", "connectionId=9007")),
            (12, "connection-silence", ("connectionId=9007", "observationMs=1000")),
            (13, "recovery-connecting", ("commandId=9023", "connectionId=9024")),
            (14, "recovery-connected", ("commandId=9023", "connectionId=9024")),
            (15, "recovery-disconnected", ("commandId=9025", "connectionId=9024")),
            (
                16,
                "recovery-connection-silence",
                ("connectionId=9024", "observationMs=1000"),
            ),
        )
        for index, state, fields in phase_fields:
            marker = markers[ordered[index]]
            if not marker.startswith(f"rf:step:{state}:passed:") or any(
                field not in marker for field in fields
            ):
                raise AssertionError(
                    f"RF passive-disconnect evidence is incomplete: {log_path}"
                )
        if any(
            field not in final
            for field in (
                "gattCommandId=9008",
                "passiveDisconnectCommandId=none",
                "recoveryConnectCommandId=9023",
                "recoveryConnectionId=9024",
                "recoveryDisconnectCommandId=9025",
                "notificationValue=ef",
            )
        ) or not final.startswith("rf-passive-disconnect:passed:"):
            raise AssertionError(
                f"RF passive-disconnect pass marker is incomplete: {log_path}"
            )
    elif connect:
        if any(marker.startswith("rf:passed:") for marker in markers):
            raise AssertionError(f"scan-only pass marker in connection run: {log_path}")
        if rssi and [marker for marker in markers if marker.startswith("rf:step:")] != [
            marker
            for marker in markers
            if any(marker.startswith(phase) for phase in phases)
        ]:
            raise AssertionError(f"unexpected RF RSSI step in {log_path}")
        if gatt and any(marker.startswith("rf-connect:passed:") for marker in markers):
            raise AssertionError(
                f"plain connection pass marker in GATT run: {log_path}"
            )
        connection_offset = (
            6
            if io
            else 5
            if indication
            else 4
            if notification
            else 1
            if gatt or rssi
            else 0
        )
        connection_evidence = [
            (markers[ordered[6]], "9005", "connecting"),
            (markers[ordered[7]], "9005", "connected"),
        ]
        if gatt:
            connection_evidence.append((markers[ordered[8]], "9008", "gatt-discovery"))
        connection_evidence.append(
            (markers[ordered[8 + connection_offset]], "9006", "disconnected")
        )
        for marker, command, state in connection_evidence:
            if any(
                field not in marker
                for field in (
                    f"commandId={command}",
                    "connectionId=9007",
                    f"peripheralId={peripheral_id}",
                    f"serviceUuid={service_uuid}",
                )
            ) or not marker.startswith(f"rf:step:{state}:passed:"):
                raise AssertionError(
                    f"RF connection correlation is incomplete: {log_path}"
                )
        silence_marker = markers[
            ordered[
                15
                if io
                else 14
                if indication
                else 13
                if notification
                else 10
                if gatt or rssi
                else 9
            ]
        ]
        if any(
            field not in silence_marker
            for field in (
                "connectionId=9007",
                f"peripheralId={peripheral_id}",
                "observationMs=1000",
            )
        ) or any(
            field not in final
            for field in (
                "connectionId=9007",
                "connectCommandId=9005",
                "disconnectCommandId=9006",
            )
        ):
            raise AssertionError(
                f"RF connection or silence evidence is incomplete: {log_path}"
            )
        if rssi:
            rssi_marker = markers[ordered[8]]
            reading = re.search(r":rssiDbm=(-?(?:0|[1-9][0-9]*))$", rssi_marker)
            summary = re.search(r":rssiDbm=(-?(?:0|[1-9][0-9]*))(?=:|$)", final)
            if (
                not rssi_marker.startswith("rf:step:rssi:passed:")
                or any(
                    field not in rssi_marker
                    for field in (
                        "commandId=9026",
                        "connectionId=9007",
                        f"peripheralId={peripheral_id}",
                        f"serviceUuid={service_uuid}",
                    )
                )
                or reading is None
                or not -32768 <= int(reading.group(1)) <= 32767
                or re.search(r":rssiCommandId=9026(?=:|$)", final) is None
                or summary is None
                or summary.group(1) != reading.group(1)
                or not final.startswith("rf-rssi:passed:")
            ):
                raise AssertionError(
                    f"RF connected RSSI correlation is incomplete: {log_path}"
                )
        if gatt:
            gatt_marker = markers[ordered[8]]
            if any(
                field not in gatt_marker
                for field in (
                    "commandId=9008",
                    "connectionId=9007",
                    "gattSessionId=9009",
                    "requestedGattSessionId=9009",
                    "serviceCount=1",
                    "characteristicCount=5",
                    "descriptorCount=2",
                )
            ):
                raise AssertionError(
                    f"RF GATT discovery evidence is incomplete: {log_path}"
                )
            discovered_services = re.search(
                r"discoveredServiceCount=([0-9]+)", gatt_marker
            )
            if discovered_services is None or int(discovered_services.group(1)) < 1:
                raise AssertionError(
                    f"RF GATT total service evidence is incomplete: {log_path}"
                )
            if io:
                io_markers = (
                    (markers[ordered[9]], "9010", "read-initial", "value=00"),
                    (markers[ordered[10]], "9011", "write-response", "value=10"),
                    (markers[ordered[11]], "9012", "read-response", "value=10"),
                    (
                        markers[ordered[12]],
                        "9013",
                        "write-without-response",
                        "value=ab",
                    ),
                    (markers[ordered[13]], "9014", "read-without-response", "value=ab"),
                )
                for marker, command, state, value in io_markers:
                    if not marker.startswith(f"rf:step:{state}:passed:") or any(
                        field not in marker
                        for field in (
                            f"commandId={command}",
                            "connectionId=9007",
                            "gattSessionId=9009",
                            f"peripheralId={peripheral_id}",
                            f"serviceUuid={service_uuid}",
                            value,
                        )
                    ):
                        raise AssertionError(
                            f"RF GATT I/O evidence is incomplete: {log_path}"
                        )
                if "terminal=local-submission" not in markers[ordered[12]]:
                    raise AssertionError(
                        f"RF no-response local terminal evidence is incomplete: {log_path}"
                    )
                if any(
                    field not in final
                    for field in (
                        "gattCommandId=9008",
                        "readCommandId=9010",
                        "writeResponseCommandId=9011",
                        "writeWithoutResponseCommandId=9013",
                    )
                ) or not final.startswith("rf-io:passed:"):
                    raise AssertionError(
                        f"RF GATT I/O pass marker is incomplete: {log_path}"
                    )
            elif notification or indication:
                if indication:
                    subscription_markers = (
                        (
                            markers[ordered[9]],
                            "indication-rejection",
                            "9018",
                            "error=not_supported",
                        ),
                        (
                            markers[ordered[10]],
                            "indication-enable",
                            "9019",
                            "actualMode=Indication",
                        ),
                        (markers[ordered[11]], "indication-value", None, "value=ef"),
                        (
                            markers[ordered[12]],
                            "indication-disable",
                            "9020",
                            "state=Disabled",
                        ),
                    )
                    subscription_id = "9021"
                else:
                    subscription_markers = (
                        (
                            markers[ordered[9]],
                            "notification-enable",
                            "9015",
                            "actualMode=Notification",
                        ),
                        (markers[ordered[10]], "notification-value", None, "value=ef"),
                        (
                            markers[ordered[11]],
                            "notification-disable",
                            "9016",
                            "state=Disabled",
                        ),
                    )
                    subscription_id = "9017"
                for marker, state, command, value in subscription_markers:
                    expected_subscription_id = (
                        "9022"
                        if indication and state == "indication-rejection"
                        else subscription_id
                    )
                    if not marker.startswith(f"rf:step:{state}:passed:") or any(
                        field not in marker
                        for field in (
                            *((f"commandId={command}",) if command else ()),
                            "connectionId=9007",
                            "gattSessionId=9009",
                            f"subscriptionId={expected_subscription_id}",
                            value,
                        )
                    ):
                        raise AssertionError(
                            f"RF notification evidence is incomplete: {log_path}"
                        )
                disable_index = 12 if indication else 11
                if "postDisableSilenceMs=2000" not in markers[ordered[disable_index]]:
                    raise AssertionError(
                        f"RF subscription stale-value silence evidence is incomplete: {log_path}"
                    )
                expected_final_fields = (
                    (
                        "gattCommandId=9008",
                        "rejectionCommandId=9018",
                        "enableCommandId=9019",
                        "disableCommandId=9020",
                        "subscriptionId=9021",
                        "indicationValue=ef",
                        "postDisableSilenceMs=2000",
                    )
                    if indication
                    else (
                        "gattCommandId=9008",
                        "enableCommandId=9015",
                        "disableCommandId=9016",
                        "subscriptionId=9017",
                        "notificationValue=ef",
                        "postDisableSilenceMs=2000",
                    )
                )
                if any(
                    field not in final for field in expected_final_fields
                ) or not final.startswith(
                    "rf-indication:passed:" if indication else "rf-notification:passed:"
                ):
                    raise AssertionError(
                        f"RF subscription pass marker is incomplete: {log_path}"
                    )
            elif "gattCommandId=9008" not in final or not final.startswith(
                "rf-gatt:passed:"
            ):
                raise AssertionError(f"RF GATT pass marker is incomplete: {log_path}")
    elif "startCommandId=9002" not in final or "stopCommandId=9003" not in final:
        raise AssertionError(f"RF command correlation is incomplete: {log_path}")


def check_rf_reload(log: str, service_uuid: str, log_path: Path) -> None:
    """Require two complete RF generations and a fixture-observed host disconnect."""
    markers = [
        line.split("BLEHUB_NITRO_RUNTIME ", 1)[1].strip()
        for line in log.splitlines()
        if "BLEHUB_NITRO_RUNTIME rf-reload:" in line
    ]
    phases = (
        "listener:registered-before-initialize",
        "initialize:passed:",
        "scan:passed:",
        "connected:passed:",
        "gatt:passed:",
        "notification-enable:passed:",
        "notification-value:passed:",
        "active:",
    )
    special = (
        "native-triggered",
        "cleanup-started",
        "fixture-host-disconnected",
        "host-complete:passed",
    )
    if len(markers) != 2 * len(phases) + len(special):
        raise AssertionError(f"missing or duplicated RF reload marker in {log_path}")
    positions: dict[str, list[int]] = {phase: [] for phase in (*phases, *special)}
    for index, marker in enumerate(markers):
        matches = [phase for phase in phases if marker.startswith("rf-reload:" + phase)]
        matches += [phase for phase in special if marker == "rf-reload:" + phase]
        if len(matches) != 1:
            raise AssertionError(f"unexpected RF reload marker in {log_path}: {marker}")
        positions[matches[0]].append(index)
    if any(len(positions[phase]) != 2 for phase in phases) or any(
        len(positions[phase]) != 1 for phase in special
    ):
        raise AssertionError(
            f"wrong RF reload generation or cleanup counts in {log_path}"
        )
    for generation in range(2):
        indices = [positions[phase][generation] for phase in phases]
        if indices != sorted(indices):
            raise AssertionError(
                f"RF reload generation {generation} out of order in {log_path}"
            )
        values = [markers[index] for index in indices]
        fields = [dict(re.findall(r":([A-Za-z]+)=([^:]+)", value)) for value in values]
        peripheral = fields[2].get("peripheralId", "")
        if (
            not re.fullmatch(r"0|[1-9][0-9]*", peripheral)
            or int(peripheral) > 18446744073709551615
            or fields[2].get("serviceUuid") != service_uuid
            or fields[3].get("peripheralId") != peripheral
            or fields[7].get("peripheralId") != peripheral
        ):
            raise AssertionError(
                f"RF reload generation {generation} peripheral/service mismatch in {log_path}"
            )
        required = (
            {"commandId": "11000"},
            {"scanSessionId": "11001"},
            {"commandId": "11005", "connectionId": "11004"},
            {"commandId": "11007", "connectionId": "11004", "gattSessionId": "11006"},
            {
                "commandId": "11009",
                "connectionId": "11004",
                "gattSessionId": "11006",
                "subscriptionId": "11008",
                "actualMode": "Notification",
            },
            {
                "connectionId": "11004",
                "gattSessionId": "11006",
                "subscriptionId": "11008",
                "value": "ef",
            },
            {"connectionId": "11004", "subscriptionId": "11008"},
        )
        for actual, expected in zip(fields[1:], required):
            if any(actual.get(key) != value for key, value in expected.items()):
                raise AssertionError(
                    f"RF reload generation {generation} correlation mismatch in {log_path}: {actual}"
                )
        if not fields[5].get("characteristicId") or not re.fullmatch(
            r"0|[1-9][0-9]*", fields[5]["characteristicId"]
        ):
            raise AssertionError(
                f"RF reload generation {generation} characteristic missing in {log_path}"
            )
    first_active = positions["active:"][0]
    second_active = positions["active:"][1]
    triggered, cleanup, disconnected, complete = (
        positions[phase][0] for phase in special
    )
    if not (
        first_active < triggered < cleanup < disconnected < second_active < complete
        and positions["listener:registered-before-initialize"][1] > cleanup
    ):
        raise AssertionError(
            f"RF reload teardown, fixture or second generation out of order in {log_path}"
        )


def check_rf_recovery(
    log: str, service_uuid: str, log_path: Path, cycles: int = 3
) -> None:
    """Require an exact, ordered recovery ledger from one listener and probe."""
    if cycles not in (3, 10):
        raise ValueError("recovery gate requires 3 or 10 cycles")
    prefix = "rf-recovery-ten" if cycles == 10 else "rf-recovery"
    markers = [
        line.split("BLEHUB_NITRO_RUNTIME ", 1)[1].strip()
        for line in log.splitlines()
        if f"BLEHUB_NITRO_RUNTIME {prefix}:" in line
    ]
    expected = [
        f"{prefix}:listener:registered-before-initialize",
        f"{prefix}:initialize:passed:commandId=10000",
    ]
    phases = (
        "start",
        "scan",
        "connected",
        "gatt",
        "notification-enable",
        "notification-value",
        "passive-disconnect",
        "old-route-silence",
        "recovery-connected",
        "recovery-disconnected",
        "cycle",
    )
    for cycle in range(cycles):
        expected.extend(
            f"{prefix}:cycle:{cycle}:{phase}" + ("" if phase == "start" else ":passed:")
            for phase in phases
        )
    expected.append(f"{prefix}:close:passed:listener-removed=true:probe-closed=true")
    # RN may drop the last JS log when the console capture ends. The close
    # marker plus the exact cycle ledger is the authoritative completion.
    if markers and markers[-1] == f"{prefix}:passed:cycles={cycles}":
        markers.pop()
    exact = {
        expected[0],
        expected[1],
        expected[-1],
        *(f"{prefix}:cycle:{cycle}:start" for cycle in range(cycles)),
    }
    if len(markers) != len(expected) or any(
        marker != prefix if prefix in exact else not marker.startswith(prefix)
        for marker, prefix in zip(markers, expected)
    ):
        raise AssertionError(
            f"missing, duplicated, wrong-cycle or out-of-order RF recovery markers in {log_path}"
        )

    for cycle in range(cycles):
        base = 2 + cycle * len(phases)
        fields = []
        for marker in markers[base + 1 : base + len(phases)]:
            parsed = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
            fields.append(parsed)
        (
            scan,
            connected,
            gatt,
            enabled,
            value,
            passive,
            old_silence,
            recovered,
            disconnected,
            summary,
        ) = fields
        base_id = 10_000 + cycle * 100

        def id_(offset: int, base: int = base_id) -> str:
            return str(base + offset)

        requirements = (
            (
                scan,
                {
                    "scanSessionId": id_(1),
                    "startCommandId": id_(2),
                    "stopCommandId": id_(3),
                    "serviceUuid": service_uuid,
                    "silenceMs": "2000",
                },
            ),
            (connected, {"commandId": id_(5), "connectionId": id_(4)}),
            (
                gatt,
                {
                    "commandId": id_(7),
                    "connectionId": id_(4),
                    "gattSessionId": id_(6),
                    "serviceCount": "1",
                    "characteristicCount": "5",
                    "descriptorCount": "2",
                },
            ),
            (
                enabled,
                {
                    "commandId": id_(9),
                    "connectionId": id_(4),
                    "gattSessionId": id_(6),
                    "subscriptionId": id_(8),
                    "actualMode": "Notification",
                },
            ),
            (
                value,
                {
                    "connectionId": id_(4),
                    "gattSessionId": id_(6),
                    "subscriptionId": id_(8),
                    "value": "ef",
                },
            ),
            (
                passive,
                {"commandId": "none", "connectionId": id_(4), "error": "connection"},
            ),
            (
                old_silence,
                {
                    "connectionId": id_(4),
                    "gattSessionId": id_(6),
                    "subscriptionId": id_(8),
                    "observationMs": "1000",
                },
            ),
            (recovered, {"commandId": id_(11), "connectionId": id_(10)}),
            (
                disconnected,
                {
                    "commandId": id_(12),
                    "connectionId": id_(10),
                    "observationMs": "1000",
                },
            ),
            (
                summary,
                {
                    "scanSessionId": id_(1),
                    "connectionId": id_(4),
                    "recoveryConnectionId": id_(10),
                },
            ),
        )
        for actual, required in requirements:
            if any(actual.get(key) != value for key, value in required.items()):
                raise AssertionError(
                    f"RF recovery cycle {cycle} correlation mismatch in {log_path}: {actual}"
                )
        peripheral = scan.get("peripheralId", "")
        if (
            not re.fullmatch(r"0|[1-9][0-9]*", peripheral)
            or int(peripheral) > 18446744073709551615
            or connected.get("peripheralId") != peripheral
            or recovered.get("peripheralId") != peripheral
        ):
            raise AssertionError(
                f"RF recovery cycle {cycle} peripheral mismatch in {log_path}"
            )
        if not enabled.get("characteristicId") or not re.fullmatch(
            r"0|[1-9][0-9]*", enabled["characteristicId"]
        ):
            raise AssertionError(
                f"RF recovery cycle {cycle} characteristic missing in {log_path}"
            )


def check_rf_multi(
    log: str,
    service_uuids: str,
    log_path: Path,
    notification_mode: str = "both",
    subscription_mode: str = "notification",
    command_offset: int = 0,
) -> None:
    """Check the private two-DK scan/connect/GATT/subscription/disconnect ledger."""
    expected_services = [
        value.strip() for value in service_uuids.split(",") if value.strip()
    ]
    if len(expected_services) != 2 or len(set(expected_services)) != 2:
        raise AssertionError(
            f"multi RF checker requires two distinct service UUIDs: {log_path}"
        )
    if notification_mode not in {"both", "only-dk-a", "only-dk-b", "reverse"}:
        raise AssertionError(f"unsupported multi RF route mode: {notification_mode}")
    if subscription_mode not in {"notification", "indication"}:
        raise AssertionError(
            f"unsupported multi RF subscription mode: {subscription_mode}"
        )
    subscription_label = (
        "indication" if subscription_mode == "indication" else "notification"
    )
    expected_actual_mode = (
        "Indication" if subscription_mode == "indication" else "Notification"
    )
    subscription_indexes = {
        "both": (0, 1),
        "only-dk-a": (0,),
        "only-dk-b": (1,),
        "reverse": (0, 1),
    }[notification_mode]
    subscription_services = {expected_services[index] for index in subscription_indexes}
    markers = [
        line.split("BLEHUB_NITRO_RUNTIME ", 1)[1].strip()
        for line in log.splitlines()
        if "BLEHUB_NITRO_RUNTIME rf-multi:" in line
    ]
    required = (
        "rf-multi:listener:registered-before-initialize",
        "rf-multi:step:initialize:passed:",
        "rf-multi:step:scan-start:passed:",
        "rf-multi:step:scan-stop:passed:",
        "rf-multi:step:silence:passed:",
        "rf-multi:step:close:passed:",
        "rf-multi:passed:",
    )
    for prefix in required:
        if sum(marker.startswith(prefix) for marker in markers) != 1:
            raise AssertionError(
                f"missing or duplicated multi RF marker {prefix}: {log_path}"
            )
    advertisements = [
        marker
        for marker in markers
        if marker.startswith("rf-multi:step:advertisement:passed:")
    ]
    connected = [
        marker
        for marker in markers
        if marker.startswith("rf-multi:step:connected:passed:")
    ]
    gatt = [
        marker
        for marker in markers
        if marker.startswith("rf-multi:step:gatt-discovery:passed:")
    ]
    io = [marker for marker in markers if marker.startswith("rf-multi:step:io:passed:")]
    rssi = [
        marker for marker in markers if marker.startswith("rf-multi:step:rssi:passed:")
    ]
    subscription_enable = [
        marker
        for marker in markers
        if marker.startswith(f"rf-multi:step:{subscription_label}-enable:passed:")
    ]
    subscription_value = [
        marker
        for marker in markers
        if marker.startswith(f"rf-multi:step:{subscription_label}-value:passed:")
    ]
    subscription_disable = [
        marker
        for marker in markers
        if marker.startswith(f"rf-multi:step:{subscription_label}-disable:passed:")
    ]
    disconnected = [
        marker
        for marker in markers
        if marker.startswith("rf-multi:step:disconnected:passed:")
    ]
    if any(
        len(group) != 2 for group in (advertisements, connected, gatt, disconnected)
    ):
        raise AssertionError(
            f"multi RF requires exactly two per-device ledger entries: {log_path}"
        )
    if any(
        len(group) != len(subscription_services)
        for group in (subscription_enable, subscription_value, subscription_disable)
    ):
        raise AssertionError(
            f"multi RF {subscription_label} coverage does not match route mode {notification_mode}: {log_path}"
        )
    if len(io) != 6:
        raise AssertionError(
            f"multi RF requires three interleaved I/O entries per device: {log_path}"
        )
    if len(rssi) != 2:
        raise AssertionError(f"multi RF requires one RSSI entry per device: {log_path}")
    peripheral_by_service: dict[str, str] = {}
    connection_by_service: dict[str, str] = {}
    session_by_service: dict[str, str] = {}
    for marker in advertisements:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        peripheral = fields.get("peripheralId", "")
        if service not in expected_services or service in peripheral_by_service:
            raise AssertionError(f"multi RF advertisement service mismatch: {log_path}")
        if (
            not re.fullmatch(r"0|[1-9][0-9]*", peripheral)
            or int(peripheral) > 18446744073709551615
        ):
            raise AssertionError(f"multi RF peripheral ID is not canonical: {log_path}")
        peripheral_by_service[service] = peripheral
    if len(set(peripheral_by_service.values())) != 2:
        raise AssertionError(f"multi RF peripherals were not distinct: {log_path}")
    for group, destination, label in (
        (connected, connection_by_service, "connected"),
        (gatt, session_by_service, "gattSessionId"),
    ):
        for marker in group:
            fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
            service = fields.get("serviceUuid", "")
            if (
                service not in peripheral_by_service
                or fields.get("peripheralId") != peripheral_by_service[service]
            ):
                raise AssertionError(
                    f"multi RF {label} peripheral correlation failed: {log_path}"
                )
            value = fields.get("connectionId" if label == "connected" else label, "")
            if not re.fullmatch(r"[1-9][0-9]*", value) or service in destination:
                raise AssertionError(
                    f"multi RF {label} identity correlation failed: {log_path}"
                )
            if label == "gattSessionId" and fields.get(
                "connectionId"
            ) != connection_by_service.get(service):
                raise AssertionError(
                    f"multi RF GATT connection correlation failed: {log_path}"
                )
            destination[service] = value
    seen_io: set[tuple[str, str]] = set()
    service_indexes = {
        service: index for index, service in enumerate(expected_services)
    }
    for marker in io:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        operation = fields.get("operation", "")
        index = service_indexes.get(service)
        if index is None or (service, operation) in seen_io:
            raise AssertionError(
                f"multi RF I/O identity duplicated or unknown: {log_path}"
            )
        seen_io.add((service, operation))
        connection = connection_by_service.get(service)
        session = session_by_service.get(service)
        if (
            fields.get("peripheralId") != peripheral_by_service.get(service)
            or fields.get("connectionId") != connection
            or fields.get("gattSessionId") != session
        ):
            raise AssertionError(
                f"multi RF I/O connection correlation failed: {log_path}"
            )
        expected_command = {
            "read-initial": str(9300 + index * 20 + command_offset),
            "write-response": str(9301 + index * 20 + command_offset),
            "readback": str(9302 + index * 20 + command_offset),
        }.get(operation)
        expected_value = {
            "read-initial": "00",
            "write-response": f"{0x10 + index * 0x10:02x}",
            "readback": f"{0x10 + index * 0x10:02x}",
        }.get(operation)
        if (
            expected_command is None
            or expected_value is None
            or fields.get("commandId") != expected_command
            or fields.get("value") != expected_value
            or not re.fullmatch(r"[1-9][0-9]*", fields.get("characteristicId", ""))
        ):
            raise AssertionError(
                f"multi RF I/O command/value correlation failed: {log_path}"
            )
    if len(seen_io) != 6:
        raise AssertionError(f"multi RF I/O coverage is incomplete: {log_path}")
    seen_rssi: set[str] = set()
    for marker in rssi:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        index = service_indexes.get(service)
        reading = fields.get("rssiDbm", "")
        if (
            index is None
            or service in seen_rssi
            or fields.get("commandId") != str(9270 + index * 10 + command_offset)
            or fields.get("peripheralId") != peripheral_by_service.get(service)
            or fields.get("connectionId") != connection_by_service.get(service)
            or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", reading)
            or not -127 <= int(reading) <= 127
        ):
            raise AssertionError(f"multi RF RSSI correlation failed: {log_path}")
        seen_rssi.add(service)
    if seen_rssi != set(expected_services):
        raise AssertionError(f"multi RF RSSI coverage is incomplete: {log_path}")
    subscriptions: dict[str, tuple[str, str]] = {}
    for marker in subscription_enable:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        index = service_indexes.get(service)
        if (
            index is None
            or service not in subscription_services
            or service in subscriptions
        ):
            raise AssertionError(
                f"multi RF {subscription_label} enable identity duplicated or unknown: {log_path}"
            )
        if (
            fields.get("commandId") != str(9560 + index * 10 + command_offset)
            or fields.get("subscriptionId") != str(9561 + index * 10 + command_offset)
            or fields.get("actualMode") != expected_actual_mode
            or fields.get("peripheralId") != peripheral_by_service.get(service)
            or fields.get("connectionId") != connection_by_service.get(service)
            or fields.get("gattSessionId") != session_by_service.get(service)
            or not re.fullmatch(r"[1-9][0-9]*", fields.get("characteristicId", ""))
        ):
            raise AssertionError(
                f"multi RF {subscription_label} enable correlation failed: {log_path}"
            )
        subscriptions[service] = (fields["subscriptionId"], fields["characteristicId"])
    if set(subscriptions) != subscription_services:
        raise AssertionError(
            f"multi RF {subscription_label} enable coverage is incomplete for {notification_mode}: {log_path}"
        )
    seen_values: set[str] = set()
    for marker in subscription_value:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        subscription = subscriptions.get(service)
        if subscription is None or service in seen_values:
            raise AssertionError(
                f"multi RF {subscription_label} value identity duplicated or unknown: {log_path}"
            )
        if (
            fields.get("subscriptionId") != subscription[0]
            or fields.get("characteristicId") != subscription[1]
            or fields.get("peripheralId") != peripheral_by_service.get(service)
            or fields.get("connectionId") != connection_by_service.get(service)
            or fields.get("gattSessionId") != session_by_service.get(service)
            or fields.get("value") != "ef"
        ):
            raise AssertionError(
                f"multi RF {subscription_label} value correlation failed: {log_path}"
            )
        seen_values.add(service)
    if seen_values != subscription_services:
        raise AssertionError(
            f"multi RF {subscription_label} value coverage is incomplete for {notification_mode}: {log_path}"
        )
    seen_disables: set[str] = set()
    for marker in subscription_disable:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        index = service_indexes.get(service)
        subscription = subscriptions.get(service)
        if index is None or subscription is None or service in seen_disables:
            raise AssertionError(
                f"multi RF {subscription_label} disable identity duplicated or unknown: {log_path}"
            )
        if (
            fields.get("commandId") != str(9580 + index * 10 + command_offset)
            or fields.get("subscriptionId") != subscription[0]
            or fields.get("characteristicId") != subscription[1]
            or fields.get("peripheralId") != peripheral_by_service.get(service)
            or fields.get("connectionId") != connection_by_service.get(service)
            or fields.get("gattSessionId") != session_by_service.get(service)
        ):
            raise AssertionError(
                f"multi RF {subscription_label} disable correlation failed: {log_path}"
            )
        seen_disables.add(service)
    if seen_disables != subscription_services:
        raise AssertionError(
            f"multi RF {subscription_label} disable coverage is incomplete for {notification_mode}: {log_path}"
        )
    for marker in disconnected:
        fields = dict(re.findall(r":([A-Za-z]+)=([^:]+)", marker))
        service = fields.get("serviceUuid", "")
        if (
            service not in peripheral_by_service
            or fields.get("peripheralId") != peripheral_by_service[service]
            or fields.get("connectionId") != connection_by_service.get(service)
        ):
            raise AssertionError(f"multi RF disconnect correlation failed: {log_path}")
    positions = {marker: index for index, marker in enumerate(markers)}
    phase_positions = (
        positions[
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi:step:scan-start:")
            )
        ],
        min(positions[marker] for marker in advertisements),
        positions[
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi:step:scan-stop:")
            )
        ],
        min(positions[marker] for marker in connected),
        min(positions[marker] for marker in gatt),
        max(positions[marker] for marker in io),
        min(positions[marker] for marker in rssi),
        min(positions[marker] for marker in subscription_enable),
        min(positions[marker] for marker in subscription_value),
        min(positions[marker] for marker in subscription_disable),
        min(positions[marker] for marker in disconnected),
        positions[
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi:step:close:")
            )
        ],
    )
    if list(phase_positions) != sorted(phase_positions):
        raise AssertionError(f"multi RF ledger phases are out of order: {log_path}")
    final = next(marker for marker in markers if marker.startswith("rf-multi:passed:"))
    if not all(
        field in final
        for field in (
            "peripheralCount=2",
            "connectionCount=2",
            "gattSessionCount=2",
            f"subscriptionMode={subscription_mode}",
        )
    ):
        raise AssertionError(f"multi RF summary is incomplete: {log_path}")


def check_rf_multi_soak(
    log: str,
    service_uuids: str,
    log_path: Path,
    cycles: int,
    notification_mode: str = "both",
    subscription_mode: str = "notification",
) -> None:
    """Check repeated full ledgers while one RN probe stays alive."""
    if cycles < 1 or cycles > 20:
        raise AssertionError(f"invalid multi RF soak cycle count: {log_path}")
    lines = log.splitlines()
    listener = "BLEHUB_NITRO_RUNTIME rf-multi:listener:registered-before-initialize"
    starts = [index for index, line in enumerate(lines) if listener in line]
    if len(starts) != cycles:
        raise AssertionError(
            f"multi RF soak expected {cycles} inner ledgers, found {len(starts)}: {log_path}"
        )
    for cycle, start in enumerate(starts):
        end = starts[cycle + 1] if cycle + 1 < len(starts) else len(lines)
        check_rf_multi(
            "\n".join(lines[start:end]),
            service_uuids,
            log_path,
            notification_mode=notification_mode,
            subscription_mode=subscription_mode,
            command_offset=cycle * 1000,
        )
    for cycle in range(1, cycles + 1):
        marker = f"BLEHUB_NITRO_RUNTIME rf-multi-soak:cycle:{cycle}:passed"
        if sum(marker in line for line in lines) != 1:
            raise AssertionError(
                f"missing multi RF soak cycle marker {cycle}: {log_path}"
            )
    required = (
        "BLEHUB_NITRO_RUNTIME rf-multi-soak:close:passed:",
        "BLEHUB_NITRO_RUNTIME rf-multi-soak:passed:",
    )
    for marker in required:
        if sum(marker in line for line in lines) != 1:
            raise AssertionError(f"missing multi RF soak marker {marker}: {log_path}")


def check_rf_multi_fault(log: str, service_uuids: str, log_path: Path) -> None:
    """Check the two-DK passive-fault, peer-survival and reconnect ledger."""
    expected = [value.strip() for value in service_uuids.split(",") if value.strip()]
    if len(expected) != 2 or len(set(expected)) != 2:
        raise AssertionError(
            f"multi fault checker requires two distinct service UUIDs: {log_path}"
        )
    markers = [
        line.split("BLEHUB_NITRO_RUNTIME ", 1)[1].strip()
        for line in log.splitlines()
        if "BLEHUB_NITRO_RUNTIME rf-multi-fault:" in line
    ]
    required = (
        "rf-multi-fault:listener:registered-before-initialize",
        "rf-multi-fault:ready-for-a-fault:",
        "rf-multi-fault:a-passive-disconnected:",
        "rf-multi-fault:b-survived-a-fault:",
        "rf-multi-fault:awaiting-a-restart",
        "rf-multi-fault:a-reconnected:",
        "rf-multi-fault:close:passed:",
        "rf-multi-fault:passed:",
    )
    for prefix in required:
        if sum(marker.startswith(prefix) for marker in markers) != 1:
            raise AssertionError(
                f"missing or duplicated multi fault marker {prefix}: {log_path}"
            )
    positions = {marker: index for index, marker in enumerate(markers)}
    ordered = [
        positions[next(marker for marker in markers if marker.startswith(prefix))]
        for prefix in required
    ]
    if ordered != sorted(ordered):
        raise AssertionError(f"multi fault ledger phases are out of order: {log_path}")
    ready = dict(
        re.findall(
            r":([A-Za-z]+)=([^:]+)",
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi-fault:ready-for-a-fault:")
            ),
        )
    )
    passive = dict(
        re.findall(
            r":([A-Za-z]+)=([^:]+)",
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi-fault:a-passive-disconnected:")
            ),
        )
    )
    survived = dict(
        re.findall(
            r":([A-Za-z]+)=([^:]+)",
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi-fault:b-survived-a-fault:")
            ),
        )
    )
    if not re.fullmatch(
        r"[1-9][0-9]*", ready.get("connectionA", "")
    ) or not re.fullmatch(r"[1-9][0-9]*", ready.get("connectionB", "")):
        raise AssertionError(
            f"multi fault initial connection identities are invalid: {log_path}"
        )
    if passive.get("connectionId") != ready.get("connectionA") or passive.get(
        "error"
    ) not in {"none", "connection"}:
        raise AssertionError(
            f"multi fault passive disconnect correlation failed: {log_path}"
        )
    if (
        survived.get("connectionId") != ready.get("connectionB")
        or survived.get("gattSessionId") != ready.get("gattB")
        or survived.get("value") != "00"
    ):
        raise AssertionError(
            f"multi fault peer-survival correlation failed: {log_path}"
        )
    reconnected = dict(
        re.findall(
            r":([A-Za-z]+)=([^:]+)",
            next(
                marker
                for marker in markers
                if marker.startswith("rf-multi-fault:a-reconnected:")
            ),
        )
    )
    if (
        not re.fullmatch(r"[1-9][0-9]*", reconnected.get("connectionId", ""))
        or not re.fullmatch(r"[1-9][0-9]*", reconnected.get("gattSessionId", ""))
        or reconnected.get("value") != "00"
    ):
        raise AssertionError(f"multi fault reconnect correlation failed: {log_path}")
    final = next(
        marker for marker in markers if marker.startswith("rf-multi-fault:passed:")
    )
    if not all(
        field in final
        for field in ("passiveFault=dk_a", "bSurvived=true", "aReconnected=true")
    ):
        raise AssertionError(f"multi fault summary is incomplete: {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("log", type=Path)
    mode = parser.add_mutually_exclusive_group()
    _ = mode.add_argument("--stress", action="store_true")
    _ = mode.add_argument("--reload", action="store_true")
    _ = mode.add_argument("--rf-service-uuid")
    _ = mode.add_argument("--rf-connect-service-uuid")
    _ = mode.add_argument("--rf-gatt-service-uuid")
    _ = mode.add_argument("--rf-io-service-uuid")
    _ = mode.add_argument("--rf-rssi-service-uuid")
    _ = mode.add_argument("--rf-notification-service-uuid")
    _ = mode.add_argument("--rf-indication-service-uuid")
    _ = mode.add_argument("--rf-passive-disconnect-service-uuid")
    _ = mode.add_argument("--rf-recovery-service-uuid")
    _ = mode.add_argument("--rf-recovery-ten-service-uuid")
    _ = mode.add_argument("--rf-reload-service-uuid")
    _ = mode.add_argument("--rf-multi-service-uuids")
    _ = mode.add_argument("--rf-multi-soak-service-uuids")
    _ = parser.add_argument(
        "--rf-multi-notification-mode",
        choices=("both", "only-dk-a", "only-dk-b", "reverse"),
        default="both",
    )
    _ = parser.add_argument(
        "--rf-multi-subscription-mode",
        choices=("notification", "indication"),
        default="notification",
    )
    _ = parser.add_argument("--rf-multi-soak-cycles", type=int, default=10)
    _ = mode.add_argument("--rf-multi-fault-service-uuids")
    args = parser.parse_args()

    log_path = cast(Path, args.log)
    stress = cast(bool, args.stress)
    reload = cast(bool, args.reload)
    rf_service_uuid = cast(str | None, args.rf_service_uuid)
    rf_connect_service_uuid = cast(str | None, args.rf_connect_service_uuid)
    rf_gatt_service_uuid = cast(str | None, args.rf_gatt_service_uuid)
    rf_io_service_uuid = cast(str | None, args.rf_io_service_uuid)
    rf_rssi_service_uuid = cast(str | None, args.rf_rssi_service_uuid)
    rf_notification_service_uuid = cast(str | None, args.rf_notification_service_uuid)
    rf_indication_service_uuid = cast(str | None, args.rf_indication_service_uuid)
    rf_passive_disconnect_service_uuid = cast(
        str | None, args.rf_passive_disconnect_service_uuid
    )
    rf_recovery_service_uuid = cast(str | None, args.rf_recovery_service_uuid)
    rf_recovery_ten_service_uuid = cast(str | None, args.rf_recovery_ten_service_uuid)
    rf_reload_service_uuid = cast(str | None, args.rf_reload_service_uuid)
    rf_multi_service_uuids = cast(str | None, args.rf_multi_service_uuids)
    rf_multi_soak_service_uuids = cast(str | None, args.rf_multi_soak_service_uuids)
    rf_multi_soak_cycles = cast(int, args.rf_multi_soak_cycles)
    rf_multi_notification_mode = cast(str, args.rf_multi_notification_mode)
    rf_multi_subscription_mode = cast(str, args.rf_multi_subscription_mode)
    rf_multi_fault_service_uuids = cast(str | None, args.rf_multi_fault_service_uuids)
    log = log_path.read_text(encoding="utf-8")
    if rf_multi_fault_service_uuids:
        check_rf_multi_fault(log, rf_multi_fault_service_uuids, log_path)
    elif rf_multi_soak_service_uuids:
        check_rf_multi_soak(
            log,
            rf_multi_soak_service_uuids,
            log_path,
            cycles=rf_multi_soak_cycles,
            notification_mode=rf_multi_notification_mode,
            subscription_mode=rf_multi_subscription_mode,
        )
    elif rf_multi_service_uuids:
        check_rf_multi(
            log,
            rf_multi_service_uuids,
            log_path,
            notification_mode=rf_multi_notification_mode,
            subscription_mode=rf_multi_subscription_mode,
        )
    elif rf_reload_service_uuid:
        check_rf_reload(log, rf_reload_service_uuid, log_path)
    elif rf_recovery_ten_service_uuid:
        check_rf_recovery(log, rf_recovery_ten_service_uuid, log_path, cycles=10)
    elif rf_recovery_service_uuid:
        check_rf_recovery(log, rf_recovery_service_uuid, log_path)
    elif rf_io_service_uuid:
        check_rf_scan(
            log, rf_io_service_uuid, log_path, connect=True, gatt=True, io=True
        )
    elif rf_passive_disconnect_service_uuid:
        check_rf_scan(
            log,
            rf_passive_disconnect_service_uuid,
            log_path,
            connect=True,
            gatt=True,
            passive_disconnect=True,
        )
    elif rf_notification_service_uuid:
        check_rf_scan(
            log,
            rf_notification_service_uuid,
            log_path,
            connect=True,
            gatt=True,
            notification=True,
        )
    elif rf_indication_service_uuid:
        check_rf_scan(
            log,
            rf_indication_service_uuid,
            log_path,
            connect=True,
            gatt=True,
            indication=True,
        )
    elif rf_gatt_service_uuid:
        check_rf_scan(log, rf_gatt_service_uuid, log_path, connect=True, gatt=True)
    elif rf_rssi_service_uuid:
        check_rf_scan(log, rf_rssi_service_uuid, log_path, connect=True, rssi=True)
    elif rf_connect_service_uuid:
        check_rf_scan(log, rf_connect_service_uuid, log_path, connect=True)
    elif rf_service_uuid:
        check_rf_scan(log, rf_service_uuid, log_path)
    elif reload:
        check_runtime_reload(log, log_path)
    else:
        expected_markers = EXPECTED_STRESS_MARKERS if stress else EXPECTED_BASIC_MARKERS
        missing = [marker for marker in expected_markers if marker not in log]
        if missing:
            raise AssertionError(f"missing runtime markers in {log_path}: {missing}")

    forbidden = [marker for marker in FORBIDDEN_MARKERS if marker in log]
    if forbidden:
        raise AssertionError(f"failed runtime markers in {log_path}: {forbidden}")

    print(f"React Native Nitro runtime gate passed: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
