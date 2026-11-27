"""Pure RN watchdog ledger; physical retained receipts belong to the host."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.peripherals.nrf52840.cancellation_control import Control

PREFIX = "BLEHUB_NITRO_RUNTIME "
SERVICE = "fd600000-0000-4000-8000-000000000000"
DISPATCH = (
    r"rf-watchdog:read-dispatched:activeCommandId=21010:queuedCommandId=21011:"
    r"connectionId=21007:gattSessionId=21009:characteristicId=([1-9][0-9]*)"
)
ACTIVE_DISPATCH = (
    r"rf-watchdog:active-dispatched:commandId=21010:connectionId=21007:"
    r"gattSessionId=21009:characteristicId=([1-9][0-9]*)"
)
TIMEOUT = "rf-watchdog:active-timeout:commandId=21010:error=timeout"
ABORTED = "rf-watchdog:queued-aborted:commandId=21011:error=operation_aborted"
AWAITING = "rf-watchdog:awaiting-release"
RETIRED = "rf-watchdog:connection-retired:connectionId=21007"
SILENCE = "rf-watchdog:late-silence:observationMs=1000"
RECOVERY = (
    r"rf-watchdog:recovery-read:passed:commandId=21024:connectionId=21021:"
    r"gattSessionId=21023:characteristicId=([1-9][0-9]*):value=00"
)
CLOSE = "rf-watchdog:close:passed"
PASSED = "rf-watchdog:passed"


class LedgerError(RuntimeError):
    """The captured projection does not prove the requested native boundaries."""


def markers(text: str) -> list[str]:
    """Strip platform log prefixes, preserving duplicates and log order."""
    result = []
    for line in text.splitlines():
        if PREFIX in line:
            body = line.split(PREFIX, 1)[1].strip()
            if body.startswith("rf-watchdog:"):
                result.append(body)
    return result


def one(lines: list[str], expression: str) -> tuple[int, re.Match[str]]:
    matches = [
        (i, m) for i, line in enumerate(lines) if (m := re.fullmatch(expression, line))
    ]
    if len(matches) != 1:
        raise LedgerError(f"expected exactly one {expression}; found {len(matches)}")
    return matches[0]


def events(lines: list[str]) -> list[tuple[int, dict[str, Any]]]:
    result = []
    for index, line in enumerate(lines):
        if line.startswith("rf-watchdog:event:"):
            try:
                event = json.loads(line[len("rf-watchdog:event:") :])
            except ValueError as error:
                raise LedgerError("malformed watchdog event JSON") from error
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                raise LedgerError("invalid watchdog event projection")
            result.append((index, event))
    return result


def category(event: dict[str, Any]) -> str | None:
    error = event.get("error")
    return error.get("category") if isinstance(error, dict) else error


def native_timeout(event: dict[str, Any]) -> bool:
    error = event.get("error")
    if not isinstance(error, dict) or error.get("category") != "timeout":
        return False
    details = error.get("platformDetails")
    message = error.get("message", "")
    if not isinstance(message, str):
        return False
    if (
        details is None
        or isinstance(details, dict)
        and details.get("platform") == "platform_apple"
    ):
        return message.startswith("Apple GATT operation watchdog expired:")
    return (
        isinstance(details, dict)
        and details.get("platform") == "platform_android"
        and details.get("name") == "AndroidBleBackend"
        and "native callback watchdog" in f"{message} {details.get('message', '')}"
    )


def release_gate(text: str) -> str:
    """Require exact markers and native (not JS outer-timeout) terminals."""
    lines = markers(text)
    dispatch, match = one(lines, DISPATCH)
    timeout, _ = one(lines, TIMEOUT)
    aborted, _ = one(lines, ABORTED)
    awaiting, _ = one(lines, AWAITING)
    if not dispatch < timeout < aborted < awaiting:
        raise LedgerError("watchdog release markers are out of order")
    projected = events(lines)
    for command, expected, summary in (
        ("21010", "timeout", timeout),
        ("21011", "operation_aborted", aborted),
    ):
        records = [(i, e) for i, e in projected if e.get("commandId") == command]
        if len(records) != 1:
            raise LedgerError(
                f"missing/duplicate/old-domain native terminal for {command}"
            )
        index, event = records[0]
        if (
            event["kind"] != "commandCompleted"
            or category(event) != expected
            or not dispatch < index < summary
        ):
            raise LedgerError(f"incorrect native terminal for {command}")
        if command == "21010" and not native_timeout(event):
            raise LedgerError("timeout lacks production native watchdog evidence")
        for field, value in (
            ("connectionId", "21007"),
            ("gattSessionId", "21009"),
            ("characteristicId", match.group(1)),
        ):
            if event.get(field) is not None and event[field] != value:
                raise LedgerError("watchdog terminal route changed")
    return match.group(1)


def verify_control(text: str, control: Control, *, complete: bool = False) -> None:
    """Exact ordered standalone barrier transcript, not merely a matching UUID somewhere."""
    from tests.peripherals.nrf52840.cancellation_control import control_markers

    observed = re.findall(r"BLEHUB_NITRO_RUNTIME (rf-control:[^\r\n]+)", text)
    expected = control_markers(control)
    if observed != expected[: len(observed)] or (complete and observed != expected):
        raise LedgerError(
            "foreign, early, duplicate or incomplete watchdog control transcript"
        )


def verify_ledger(
    text: str,
    *,
    characteristic: str,
    service_uuid: str = SERVICE,
    integrated: bool = False,
) -> dict[str, Any]:
    """Check the finished RN event projection, including all thirteen commands.

    Native callbacks may complete a command before its domain reaches JS; the
    checker does not impose a stronger domain-before-terminal guarantee. Failed
    active/queued commands must have no domain and exactly one terminal each.
    """
    if release_gate(text) != characteristic:
        raise LedgerError("dispatch characteristic changed after WAITING")
    lines = markers(text)
    required = [
        DISPATCH,
        TIMEOUT,
        ABORTED,
        AWAITING,
        RETIRED,
        SILENCE,
        RECOVERY,
        CLOSE,
        PASSED,
    ]
    boundaries = [one(lines, expression)[0] for expression in required]
    if boundaries != sorted(boundaries) or len(set(boundaries)) != len(boundaries):
        raise LedgerError("watchdog final markers are out of order")
    projected = events(lines)
    if len(projected) > 256:
        raise LedgerError("watchdog event ledger overflow")
    consumed = set(boundaries) | {i for i, _ in projected}
    if integrated:
        discovery = one(
            lines,
            r"rf-watchdog:discovery-ready:connectionId=21007:gattSessionId=21009:characteristicId=([1-9][0-9]*)",
        )
        active = one(lines, ACTIVE_DISPATCH)
        if (
            not discovery[0] < active[0] < boundaries[0]
            or discovery[1].group(1) != characteristic
            or active[1].group(1) != characteristic
        ):
            raise LedgerError("integrated discovery/active route order drift")
        consumed.update((discovery[0], active[0]))
    if len(consumed) != len(lines):
        raise LedgerError("unexpected watchdog marker")
    new_char = one(lines, RECOVERY)[1].group(1)
    for char in (characteristic, new_char):
        if int(char) > (1 << 64) - 1:
            raise LedgerError("characteristic ID is outside u64")
    # command -> (required domains, optional domains, route, lower/upper boundary)
    rules: dict[str, tuple[set[str], set[str], dict[str, str], int, int]] = {
        "21001": (set(), set(), {}, -1, boundaries[0]),
        "21002": (
            {"scanStarted"},
            set(),
            {"scanSessionId": "21004"},
            -1,
            boundaries[0],
        ),
        "21003": (
            {"scanStopped"},
            set(),
            {"scanSessionId": "21004"},
            -1,
            boundaries[0],
        ),
        "21005": (
            {"connectionStateChanged:Connecting", "connectionStateChanged:Connected"},
            set(),
            {"connectionId": "21007"},
            -1,
            boundaries[0],
        ),
        "21008": (
            {"gattDiscovered"},
            set(),
            {"connectionId": "21007", "gattSessionId": "21009"},
            -1,
            boundaries[0],
        ),
        "21010": (
            set(),
            set(),
            {
                "connectionId": "21007",
                "gattSessionId": "21009",
                "characteristicId": characteristic,
            },
            boundaries[0],
            boundaries[1],
        ),
        "21011": (
            set(),
            set(),
            {
                "connectionId": "21007",
                "gattSessionId": "21009",
                "characteristicId": characteristic,
            },
            boundaries[0],
            boundaries[2],
        ),
        "21012": (
            {"scanStarted"},
            set(),
            {"scanSessionId": "21014"},
            boundaries[5],
            boundaries[6],
        ),
        "21013": (
            {"scanStopped"},
            set(),
            {"scanSessionId": "21014"},
            boundaries[5],
            boundaries[6],
        ),
        "21020": (
            {"connectionStateChanged:Connecting", "connectionStateChanged:Connected"},
            set(),
            {"connectionId": "21021"},
            boundaries[5],
            boundaries[6],
        ),
        "21022": (
            {"gattDiscovered"},
            set(),
            {"connectionId": "21021", "gattSessionId": "21023"},
            boundaries[5],
            boundaries[6],
        ),
        "21024": (
            {"characteristicRead"},
            set(),
            {
                "connectionId": "21021",
                "gattSessionId": "21023",
                "characteristicId": new_char,
            },
            boundaries[5],
            boundaries[6],
        ),
        "21025": (
            {"connectionStateChanged:Disconnected"},
            {"connectionStateChanged:Disconnecting"},
            {"connectionId": "21021"},
            boundaries[6],
            boundaries[7],
        ),
    }
    terminal_ids: set[str] = set()
    domain_ids: set[tuple[str, str]] = set()
    losses: list[int] = []
    ready = False
    matching_peripheral: str | None = None
    recovery_advertisement = False
    active_scan: str | None = None
    scan_starts = {"21002": "21004", "21012": "21014"}
    scan_stops = {"21003": "21004", "21013": "21014"}
    predecessors = {
        "21002": "21001",
        "21003": "21002",
        "21005": "21003",
        "21008": "21005",
        "21010": "21008",
        "21011": "21008",
        "21012": "21011",
        "21013": "21012",
        "21020": "21013",
        "21022": "21020",
        "21024": "21022",
        "21025": "21024",
    }
    recovery_retired = False
    terminals: dict[str, Any] = {}

    def complete(command: str) -> bool:
        return command in terminal_ids and all(
            (command, domain) in domain_ids for domain in rules[command][0]
        )

    for index, event in projected:
        kind, command = event["kind"], event.get("commandId")
        if kind == "adapterSnapshotChanged":
            if (
                index >= boundaries[0]
                or event.get("adapterState") != "PoweredOn"
                or event.get("scanAuthorization") != "Authorized"
                or command not in (None, "21001")
                or event.get("error")
                or command == "21001"
                and (command, kind) in domain_ids
            ):
                raise LedgerError("adapter was not ready or snapshot was duplicated")
            if command is not None:
                domain_ids.add((command, kind))
            ready = True
            continue
        if kind == "advertisementObserved":
            peripheral = event.get("peripheralId")
            services = event.get("advertisedServices", [])
            if (
                active_scan is None
                or command is not None
                or event.get("error")
                or event.get("scanSessionId") != active_scan
                or not isinstance(peripheral, str)
                or re.fullmatch(r"0|[1-9][0-9]*", peripheral) is None
                or int(peripheral) > (1 << 64) - 1
                or not isinstance(services, list)
            ):
                raise LedgerError("late or uncorrelated advertisement")
            if service_uuid in services:
                if active_scan == "21004" and matching_peripheral is None:
                    matching_peripheral = peripheral
                elif active_scan == "21014" and peripheral == matching_peripheral:
                    recovery_advertisement = True
            continue
        if kind == "connectionStateChanged" and command is None:
            error = event.get("error")
            android_loss = (
                isinstance(error, dict)
                and error.get("category") == "connection"
                and isinstance(error.get("platformDetails"), dict)
                and error["platformDetails"].get("platform") == "platform_android"
            )
            if not (
                boundaries[0] < index < boundaries[4]
                and event.get("connectionId") == "21007"
                and not losses
                and (
                    event.get("connectionState") == "Disconnected"
                    and not error
                    or event.get("connectionState") is None
                    and android_loss
                )
            ):
                raise LedgerError("late/duplicate/wrong passive retirement")
            losses.append(index)
            continue
        if command not in rules:
            raise LedgerError(f"unknown native command/domain: {kind}:{command}")
        domains, optional, route, lower, upper = rules[command]
        if not lower < index < upper:
            raise LedgerError("late old-domain or out-of-phase native event")
        previous = predecessors.get(command)
        if previous is not None and not complete(previous):
            raise LedgerError("native command precedes the UI's completed prior phase")
        if command == "21002" and not ready:
            raise LedgerError("scan started before adapter readiness")
        if command == "21003" and matching_peripheral is None:
            raise LedgerError("initial scan stopped without a matching advertisement")
        if command == "21012" and not complete("21010"):
            raise LedgerError("recovery scan precedes the active native timeout")
        if command == "21013" and not recovery_advertisement:
            raise LedgerError(
                "recovery scan stopped without the original peripheral re-advertising"
            )
        if kind != "commandCompleted" and (
            losses
            and event.get("connectionId") == "21007"
            or recovery_retired
            and event.get("connectionId") == "21021"
        ):
            raise LedgerError("old domain after connection retirement")
        for field in (
            "connectionId",
            "gattSessionId",
            "characteristicId",
            "scanSessionId",
        ):
            if (
                kind != "commandCompleted"
                and field in route
                or event.get(field) is not None
            ) and event.get(field) != route.get(field):
                raise LedgerError("native event route mismatch")
        if kind == "commandCompleted":
            expected = {"21010": "timeout", "21011": "operation_aborted"}.get(command)
            if command in terminal_ids or category(event) != expected:
                raise LedgerError("duplicate or incorrect native terminal")
            terminal_ids.add(command)
            terminals[command] = event
        else:
            key = (
                f"{kind}:{event.get('connectionState')}"
                if kind == "connectionStateChanged"
                else kind
            )
            if (
                key not in domains | optional
                or (command, key) in domain_ids
                or category(event)
            ):
                raise LedgerError("unexpected/duplicate/failed native domain")
            domain_ids.add((command, key))
            if kind == "scanStopped":
                if active_scan != scan_stops[command]:
                    raise LedgerError("scan stop has no matching active scan")
                active_scan = None
            if command == "21025" and event.get("connectionState") == "Disconnected":
                recovery_retired = True
            if command == "21024":
                value = event.get("value")
                if (
                    not isinstance(value, list)
                    or len(value) != 1
                    or type(value[0]) is not int
                    or value[0] != 0
                ):
                    raise LedgerError(
                        "recovery value must be exactly the UI byte array [0]"
                    )
        if command in scan_starts:
            session = scan_starts[command]
            if active_scan is not None and active_scan != session:
                raise LedgerError("watchdog scans overlap")
            active_scan = session
    if (
        terminal_ids != set(rules)
        or not losses
        or not ready
        or matching_peripheral is None
        or not recovery_advertisement
        or active_scan is not None
    ):
        raise LedgerError(
            "missing native terminals, readiness, advertisement or retirement"
        )
    if any(
        (command, domain) not in domain_ids
        for command, (domains, _, _, _, _) in rules.items()
        for domain in domains
    ):
        raise LedgerError("missing native domain")
    return {
        "native_terminals": terminals,
        "recovery_characteristic": new_char,
        "recovery_peripheral": matching_peripheral,
        "recovery_scan_session": "21014",
    }
