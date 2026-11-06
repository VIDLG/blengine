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
    expected_local_name: str | None = None,
) -> None:
    markers = [
        line.split("BLEHUB_NITRO_RUNTIME ", 1)[1].strip()
        for line in log.split("\n")
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

    def has_fields(marker: str, fields: tuple[str, ...]) -> bool:
        if not io:
            return all(field in marker for field in fields)
        # IO scalar tokens must match exactly once, including their key boundary.
        # Do not parse free-form names or claim native topology from these reports.
        tokens = marker.split(":")
        return all(
            [
                token
                for token in tokens
                if token.startswith(field.split("=", 1)[0] + "=")
            ]
            == [field]
            for field in fields
        )

    if io:
        for index, fields in (
            (
                1,
                (
                    "commandId=9001",
                    "adapterState=PoweredOn",
                    "scanAuthorization=Authorized",
                ),
            ),
            (2, ("startCommandId=9002", "scanSessionId=9004")),
            (4, ("stopCommandId=9003", "scanSessionId=9004")),
            (5, ("scanSessionId=9004",)),
            (-2, ("probe-closed=true", "listener-removed=true")),
        ):
            if not has_fields(markers[ordered[index]], fields):
                raise AssertionError(
                    f"RF I/O boundary fields are incomplete: {log_path}"
                )
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
    if expected_local_name is not None:
        fixture_start = (
            f"rf:start:serviceUuid={service_uuid}:"
            f"expectedLocalName={expected_local_name}:initCommandId=9001:"
        )
        if sum(marker.startswith(fixture_start) for marker in markers) != 1:
            raise AssertionError(
                f"RF launch fixture metadata is missing or mismatched: {log_path}"
            )
        local_name = re.search(r":localName=(.*?):filterServiceUuid=", advertisement)
        if local_name is None or local_name.group(1) not in (
            expected_local_name,
            "missing",
        ):
            raise AssertionError(
                f"RF local name does not match the fixture: {log_path}"
            )
    advertisement_fields = (
        f"filterServiceUuid={service_uuid}",
        f"payloadAdvertisedServices={payload.group(1)}",
        f"payloadContainsFilterUuid={payload.group(2)}",
        "scanSessionId=9004",
        f"peripheralId={peripheral_id}",
    )
    if not has_fields(advertisement, advertisement_fields):
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
    if not has_fields(final, final_fields):
        raise AssertionError(
            f"RF result does not match the observed peripheral: {log_path}"
        )
    if not has_fields(markers[ordered[4]], ("requested=true",)) or not has_fields(
        markers[ordered[5]], ("observationMs=2000",)
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
            if not has_fields(
                marker,
                (
                    f"commandId={command}",
                    "connectionId=9007",
                    f"peripheralId={peripheral_id}",
                    f"serviceUuid={service_uuid}",
                ),
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
        if not has_fields(
            silence_marker,
            (
                "connectionId=9007",
                f"peripheralId={peripheral_id}",
                "observationMs=1000",
                *((f"serviceUuid={service_uuid}",) if io else ()),
            ),
        ) or not has_fields(
            final,
            (
                "connectionId=9007",
                "connectCommandId=9005",
                "disconnectCommandId=9006",
            ),
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
            if not has_fields(
                gatt_marker,
                (
                    "commandId=9008",
                    "connectionId=9007",
                    "gattSessionId=9009",
                    "requestedGattSessionId=9009",
                    "serviceCount=1",
                    "characteristicCount=5",
                    "descriptorCount=2",
                ),
            ):
                raise AssertionError(
                    f"RF GATT discovery evidence is incomplete: {log_path}"
                )
            discovered_services = re.search(
                r"discoveredServiceCount=([0-9]+)", gatt_marker
            )
            if (
                discovered_services is None
                or int(discovered_services.group(1)) < 1
                or not has_fields(
                    gatt_marker,
                    (f"discoveredServiceCount={discovered_services.group(1)}",),
                )
            ):
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
                characteristic_id = None
                for marker, command, state, value in io_markers:
                    route = re.search(
                        r"(?:^|:)characteristicId=([^:\s]+)(?=:|$)", marker
                    )
                    if route is None:
                        raise AssertionError(
                            f"RF GATT I/O characteristic route is missing: {log_path}"
                        )
                    if characteristic_id is None:
                        characteristic_id = route.group(1)
                    if not marker.startswith(
                        f"rf:step:{state}:passed:"
                    ) or not has_fields(
                        marker,
                        (
                            f"commandId={command}",
                            "connectionId=9007",
                            "gattSessionId=9009",
                            f"peripheralId={peripheral_id}",
                            f"serviceUuid={service_uuid}",
                            f"characteristicId={characteristic_id}",
                            value,
                        ),
                    ):
                        raise AssertionError(
                            f"RF GATT I/O evidence is incomplete: {log_path}"
                        )
                if not has_fields(markers[ordered[12]], ("terminal=local-submission",)):
                    raise AssertionError(
                        f"RF no-response local terminal evidence is incomplete: {log_path}"
                    )
                if not has_fields(
                    final,
                    (
                        "gattCommandId=9008",
                        "readCommandId=9010",
                        "writeResponseCommandId=9011",
                        "writeWithoutResponseCommandId=9013",
                    ),
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


def check_rf_cancellation(
    log: str,
    service_uuid: str,
    log_path: Path,
    *,
    expected_local_name: str | None = None,
) -> None:
    """Validate cancellation evidence on top of the canonical GATT flow."""
    runtime_prefix = "BLEHUB_NITRO_RUNTIME "
    cancellation_steps = (
        (
            "cancellation-active-rejection",
            ("targetCommandId=9027", "category=operation_not_cancellable"),
        ),
        ("cancellation-queued-accepted", ("targetCommandId=9028",)),
        ("cancellation-terminal", ("commandId=9028", "error=cancelled")),
        ("cancellation-active-completion", ("commandId=9027",)),
        ("cancellation-late-silence", ("commandId=9028", "observationMs=1000")),
        (
            "cancellation-unknown-target",
            ("targetCommandId=9030", "category=operation_not_found"),
        ),
        (
            "cancellation-completed-target",
            ("targetCommandId=9027", "category=operation_not_found"),
        ),
        ("cancellation-recovery", ("commandId=9029",)),
    )
    lines = log.splitlines()
    step_markers = [
        line.split(runtime_prefix, 1)[1].strip()
        for line in lines
        if runtime_prefix in line and " rf:step:cancellation-" in line
    ]
    if len(step_markers) != len(cancellation_steps):
        raise AssertionError(
            f"RF cancellation steps are missing or duplicated: {log_path}"
        )
    for marker, (state, fields) in zip(step_markers, cancellation_steps, strict=True):
        if not marker.startswith(f"rf:step:{state}:passed:") or any(
            field not in marker for field in fields
        ):
            raise AssertionError(f"RF cancellation step is incomplete: {log_path}")
    step_positions = {
        state: next(
            index
            for index, marker in enumerate(step_markers)
            if marker.startswith(f"rf:step:{state}:")
        )
        for state, _ in cancellation_steps
    }
    if not (
        step_positions["cancellation-queued-accepted"]
        < step_positions["cancellation-terminal"]
        and step_positions["cancellation-active-completion"]
        < step_positions["cancellation-recovery"]
    ):
        raise AssertionError(f"RF cancellation steps are out of order: {log_path}")

    event_prefix = f"{runtime_prefix}rf-cancellation:event:"
    event_lines = [
        line.split(event_prefix, 1)[1].strip() for line in lines if event_prefix in line
    ]
    if len(event_lines) != 1:
        raise AssertionError(
            f"RF cancellation event ledger is missing or duplicated: {log_path}"
        )
    fields: dict[str, list[str]] = {}
    for field in event_lines[0].split(":"):
        name, separator, values = field.partition("=")
        if not separator or not name or name in fields:
            raise AssertionError(
                f"RF cancellation event ledger is malformed: {log_path}"
            )
        fields[name] = values.split("|")
    expected_names = (
        "kind",
        "commandId",
        "connectionId",
        "gattSessionId",
        "characteristicId",
        "error",
        "value",
    )
    if tuple(fields) != expected_names or any(
        len(fields[name]) != 5 for name in expected_names
    ):
        raise AssertionError(
            f"RF cancellation event ledger does not contain five events: {log_path}"
        )
    records = [
        dict(zip(fields, values, strict=True))
        for values in zip(*fields.values(), strict=True)
    ]
    required_records = {
        ("characteristicRead", "9027", "ok"),
        ("commandCompleted", "9027", "ok"),
        ("commandCompleted", "9028", "cancelled"),
        ("characteristicRead", "9029", "ok"),
        ("commandCompleted", "9029", "ok"),
    }
    actual_records = {
        (record["kind"], record["commandId"], record["error"]) for record in records
    }
    if actual_records != required_records or len(actual_records) != len(records):
        raise AssertionError(
            f"RF cancellation event order or correlation is invalid: {log_path}"
        )
    for record in records:
        is_read = record["kind"] == "characteristicRead"
        if record["kind"] not in {"characteristicRead", "commandCompleted"}:
            raise AssertionError(f"RF cancellation event kind is invalid: {log_path}")
        if is_read:
            if (
                record["connectionId"] != "9007"
                or record["gattSessionId"] != "9009"
                or record["characteristicId"] == "none"
                or re.fullmatch(r"[0-9a-fA-F]+", record["value"] or "") is None
            ):
                raise AssertionError(
                    f"RF cancellation read domain is incomplete: {log_path}"
                )
        elif any(
            record[name] != "none"
            for name in ("connectionId", "gattSessionId", "characteristicId", "value")
        ):
            raise AssertionError(
                f"RF cancellation terminal leaked domain state: {log_path}"
            )
    active_read = next(
        index
        for index, record in enumerate(records)
        if record["kind"] == "characteristicRead" and record["commandId"] == "9027"
    )
    active_terminal = next(
        index
        for index, record in enumerate(records)
        if record["kind"] == "commandCompleted" and record["commandId"] == "9027"
    )
    recovery_read = next(
        index
        for index, record in enumerate(records)
        if record["kind"] == "characteristicRead" and record["commandId"] == "9029"
    )
    recovery_terminal = next(
        index
        for index, record in enumerate(records)
        if record["kind"] == "commandCompleted" and record["commandId"] == "9029"
    )
    if not active_read < active_terminal < recovery_read < recovery_terminal:
        raise AssertionError(
            f"RF cancellation read completion order is invalid: {log_path}"
        )
    characteristic_ids = [
        record["characteristicId"]
        for record in records
        if record["kind"] == "characteristicRead"
    ]
    if (
        any(
            not re.fullmatch(r"[1-9][0-9]*", value)
            or len(value) > 20
            or (len(value) == 20 and value > "18446744073709551615")
            for value in characteristic_ids
        )
        or len(set(characteristic_ids)) != 1
    ):
        raise AssertionError(
            f"RF cancellation characteristic identity is invalid: {log_path}"
        )

    normalized_lines: list[str] = []
    for line in lines:
        if event_prefix in line or " rf:step:cancellation-" in line:
            continue
        normalized_lines.append(
            line.replace(
                f"{runtime_prefix}rf-cancellation:passed:",
                f"{runtime_prefix}rf-gatt:passed:",
                1,
            )
        )
    check_rf_scan(
        "\n".join(normalized_lines),
        service_uuid,
        log_path,
        connect=True,
        gatt=True,
        expected_local_name=expected_local_name,
    )
    final_lines = [
        line.split(runtime_prefix, 1)[1].strip()
        for line in lines
        if f"{runtime_prefix}rf-cancellation:passed:" in line
    ]
    if len(final_lines) != 1:
        raise AssertionError(
            f"RF cancellation pass marker is missing or duplicated: {log_path}"
        )
    final = final_lines[0]
    advertisement = next(
        line.split(runtime_prefix, 1)[1].strip()
        for line in lines
        if f"{runtime_prefix}rf:step:advertisement:passed:" in line
    )
    peripheral_match = re.search(r"peripheralId=([0-9]+)", advertisement)
    peripheral_id = peripheral_match.group(1) if peripheral_match else ""
    final_fields = (
        f"serviceUuid={service_uuid}",
        f"peripheralId={peripheral_id}",
        "connectionId=9007",
        "connectCommandId=9005",
        "gattCommandId=9008",
        "disconnectCommandId=9006",
        "scanSessionId=9004",
    )
    if not final.startswith("rf-cancellation:passed:") or any(
        field not in final for field in final_fields
    ):
        raise AssertionError(f"RF cancellation pass marker is incomplete: {log_path}")


def check_rf_lab_suite(
    log: str,
    service_uuid: str,
    log_path: Path,
    *,
    expected_local_name: str | None = None,
    cancellation_evidence: Path | None = None,
) -> None:
    """Validate ten flows in one runtime plus both same-session physical holds."""
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
        "rf-watchdog",
    )
    starts = list(
        re.finditer(
            r"BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=([0-9]+):mode=([^\s]+)",
            log,
        )
    )
    if len(starts) != len(modes):
        raise AssertionError(f"suite flow-started coverage is incomplete: {log_path}")
    for index, (expected_mode, match) in enumerate(zip(modes, starts, strict=True)):
        if match.group(1) != str(index) or match.group(2) != expected_mode:
            raise AssertionError(f"suite flow order is invalid: {log_path}")
        end = starts[index + 1].start() if index + 1 < len(starts) else len(log)
        segment = log[match.start() : end]
        passed = f"rf-lab-suite:flow-passed:index={index}:mode={expected_mode}"
        failed = f"rf-lab-suite:flow-failed:index={index}:mode={expected_mode}:"
        if segment.count(passed) != 1 or failed in segment:
            raise AssertionError(
                f"suite flow {expected_mode} did not pass exactly once: {log_path}"
            )
        if expected_mode == "rf-watchdog":
            from tests.peripherals.nrf52840.continuous_watchdog import (
                check_physical_evidence as check_watchdog_physical,
            )

            if cancellation_evidence is None:
                raise AssertionError("continuous10 requires both physical hold reports")
            try:
                check_watchdog_physical(
                    cancellation_evidence.parent.parent
                    / "flow-9-rf-watchdog"
                    / "physical-evidence.json",
                    segment,
                    cancellation_evidence,
                    service_uuid,
                )
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                raise AssertionError(
                    f"physical watchdog evidence invalid: {error}"
                ) from error
        elif expected_mode == "rf-cancellation":
            check_rf_cancellation(
                segment, service_uuid, log_path, expected_local_name=expected_local_name
            )
            from tests.peripherals.nrf52840.cancellation_control import (
                check_physical_evidence,
            )

            try:
                check_physical_evidence(cancellation_evidence, segment, service_uuid)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                raise AssertionError(
                    f"physical cancellation evidence invalid: {error}"
                ) from error
        else:
            check_rf_scan(
                segment,
                service_uuid,
                log_path,
                connect=expected_mode != "rf",
                gatt=expected_mode
                in {
                    "rf-gatt",
                    "rf-io",
                    "rf-notification",
                    "rf-indication",
                    "rf-passive-disconnect",
                },
                io=expected_mode == "rf-io",
                rssi=expected_mode == "rf-rssi",
                notification=expected_mode
                in {"rf-notification", "rf-passive-disconnect"},
                indication=expected_mode == "rf-indication",
                passive_disconnect=expected_mode == "rf-passive-disconnect",
                expected_local_name=expected_local_name,
            )
    passes = re.findall(
        r"BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-passed:index=([0-9]+):mode=([^\s:]+)",
        log,
    )
    if (
        passes != [(str(index), mode) for index, mode in enumerate(modes)]
        or "BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-failed:" in log
    ):
        raise AssertionError("continuous10 flow terminals missing/duplicated/failed")
    runtime = re.findall(
        r"BLEHUB_NITRO_RUNTIME rf-lab-suite:runtime:session=([0-9a-f-]+):nonce=([0-9a-z-]+):phase=(start|close)",
        log,
    )
    if (
        len(runtime) != 2
        or runtime[0][:2] != runtime[1][:2]
        or [r[2] for r in runtime] != ["start", "close"]
    ):
        raise AssertionError("continuous10 JS runtime identity missing/changed")
    from tests.peripherals.nrf52840.cancellation_control import read_document

    if (
        cancellation_evidence is None
        or runtime[0][0] != read_document(cancellation_evidence)["session_id"]
    ):
        raise AssertionError("continuous10 JS/provider session drift")
    summary = "rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0"
    summaries = re.findall(
        r"BLEHUB_NITRO_RUNTIME (rf-lab-suite:(?:passed|failed):flows=[^\r\n]+)", log
    )
    if summaries != [summary]:
        raise AssertionError(f"suite summary is missing or duplicated: {log_path}")
    runtime_start = log.index(
        f"rf-lab-suite:runtime:session={runtime[0][0]}:nonce={runtime[0][1]}:phase=start"
    )
    runtime_close = log.index(
        f"rf-lab-suite:runtime:session={runtime[1][0]}:nonce={runtime[1][1]}:phase=close"
    )
    final_flow = log.index("rf-lab-suite:flow-passed:index=9:mode=rf-watchdog")
    if (
        not runtime_start
        < starts[0].start()
        < final_flow
        < runtime_close
        < log.index(summary)
    ):
        raise AssertionError("continuous10 runtime/summary boundary order drift")
    from tools.react_native.rf_control_relay import check_suite_identity

    try:
        check_suite_identity(log, log_path, cancellation_evidence.parent.parent)
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        IndexError,
    ) as error:
        raise AssertionError(
            f"continuous native runtime identity invalid: {error}"
        ) from error


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
    _ = mode.add_argument("--rf-cancellation-service-uuid")
    _ = mode.add_argument("--rf-lab-suite-service-uuid")
    _ = parser.add_argument("--rf-expected-local-name")
    _ = parser.add_argument("--rf-cancellation-evidence", type=Path)
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
    if args.rf_expected_local_name is not None and (
        args.rf_lab_suite_service_uuid is None or not args.rf_expected_local_name
    ):
        parser.error(
            "--rf-expected-local-name requires suite service UUID and a nonempty name"
        )

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
    rf_cancellation_service_uuid = cast(str | None, args.rf_cancellation_service_uuid)
    rf_lab_suite_service_uuid = cast(str | None, args.rf_lab_suite_service_uuid)
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
    elif rf_cancellation_service_uuid:
        check_rf_cancellation(log, rf_cancellation_service_uuid, log_path)
    elif rf_lab_suite_service_uuid:
        check_rf_lab_suite(
            log,
            rf_lab_suite_service_uuid,
            log_path,
            expected_local_name=args.rf_expected_local_name,
            cancellation_evidence=args.rf_cancellation_evidence,
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
