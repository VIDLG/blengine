"""Pure checker for the two-generation RN pending-ATT read ledger.

The checker consumes captured ``BLEHUB_NITRO_RUNTIME`` marker text only.  It
performs no device, process, serial, React Native, or firmware work, so a later
controller can use it after its hardware-specific orchestration has completed.

A passing result proves the observed marker ledger, not that a CoreBluetooth
late callback was delivered or that a native queue was drained.  Those remain
separate evidence boundaries for the caller.
"""

from __future__ import annotations

import re
import uuid
from re import Match

PREFIX = "BLEHUB_NITRO_RUNTIME "
MODE = "rf-pending-read-reload"
SERVICE = "fd100000-0000-4000-8000-000000000000"

OLD = (
    r"generation=1:read-dispatched:commandId=12008:connectionId=12004:"
    r"gattSessionId=12006:characteristicId=([1-9][0-9]*):peripheralId="
    r"(0|[1-9][0-9]*)"
)
NEW = (
    r"generation=2:read:passed:commandId=13008:connectionId=13004:"
    r"gattSessionId=13006:characteristicId=([1-9][0-9]*):value=00"
)
AWAITING_ARM = (
    r"generation=2:awaiting-arm:commandId=13008:connectionId=13004:"
    r"gattSessionId=13006:characteristicId=([1-9][0-9]*)"
)
READ_DISPATCHED = (
    r"generation=2:read-dispatched:commandId=13008:connectionId=13004:"
    r"gattSessionId=13006:characteristicId=([1-9][0-9]*)"
)
UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NATIVE = (
    r"native:(?P<event>read-accepted|dispose-returned|late-submit-rejected):"
    rf"runId=(?P<run_id>{UUID_TEXT}):probeId=(?P<probe_id>{UUID_TEXT}):"
    r"commandId=(?P<command_id>0|[1-9][0-9]*):"
    r"connectionId=(?P<connection_id>0|[1-9][0-9]*):"
    r"gattSessionId=(?P<gatt_id>0|[1-9][0-9]*):"
    r"characteristicId=(?P<characteristic>[1-9][0-9]*)"
)
NATIVE_EVENTS = ("read-accepted", "dispose-returned", "late-submit-rejected")

_U64_MAX = (1 << 64) - 1

__all__ = [
    "AWAITING_ARM",
    "MODE",
    "NATIVE",
    "NATIVE_EVENTS",
    "NEW",
    "OLD",
    "PREFIX",
    "READ_DISPATCHED",
    "SERVICE",
    "markers",
    "native_boundaries",
    "one",
    "verify_ledger",
]


def markers(text: str) -> list[str]:
    """Return only this checker mode's structured marker bodies, in log order."""
    return [
        line[len(PREFIX) :]
        for line in text.splitlines()
        if line.startswith(PREFIX + MODE + ":")
    ]


def one(lines: list[str], expression: str) -> tuple[int, Match[str]]:
    """Require exactly one full-line match and return its index and match."""
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := re.fullmatch(expression, line))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {expression}, found {len(matches)}")
    return matches[0]


def _canonical_uuid(value: str, label: str) -> None:
    if re.fullmatch(UUID_TEXT, value) is None or str(uuid.UUID(value)) != value:
        raise RuntimeError(f"{label} must be a canonical lowercase UUID")


def _u64(value: str, label: str, *, positive: bool = False) -> int:
    pattern = r"[1-9][0-9]*" if positive else r"(?:0|[1-9][0-9]*)"
    if re.fullmatch(pattern, value) is None:
        raise RuntimeError(f"{label} is not a canonical u64")
    number = int(value)
    if number > _U64_MAX or (positive and number == 0):
        raise RuntimeError(f"{label} is outside the u64 range")
    return number


def native_boundaries(
    lines: list[str], *, run_id: str, probe_id: str | None = None, complete: bool
) -> dict[str, tuple[int, Match[str]]]:
    """Parse and correlate the private native pending-read boundary markers.

    All three markers must carry one identical run/probe/read identity.  With
    ``complete=False`` only the initial native acceptance is required; the
    final ledger uses ``complete=True`` and requires every boundary exactly
    once.
    """
    _canonical_uuid(run_id, "run_id")
    if probe_id is not None:
        _canonical_uuid(probe_id, "probe_id")

    native_lines: list[tuple[int, Match[str]]] = []
    for index, line in enumerate(lines):
        if line.startswith(MODE + ":native-boundary-"):
            raise RuntimeError("native boundary reported failure")
        if line.startswith(MODE + ":native:"):
            match = re.fullmatch(MODE + ":" + NATIVE, line)
            if match is None:
                raise RuntimeError("unknown or malformed native marker")
            native_lines.append((index, match))

    by_event: dict[str, list[tuple[int, Match[str]]]] = {
        event: [] for event in NATIVE_EVENTS
    }
    for index, match in native_lines:
        event = match.group("event")
        by_event[event].append((index, match))
        if match.group("run_id") != run_id:
            raise RuntimeError("native marker run_id changed")
        marker_probe = match.group("probe_id")
        _canonical_uuid(marker_probe, "native marker probe_id")
        if probe_id is not None and marker_probe != probe_id:
            raise RuntimeError("native marker probe_id changed")
        _u64(match.group("command_id"), "native command ID")
        _u64(match.group("connection_id"), "native connection ID")
        _u64(match.group("gatt_id"), "native GATT session ID")
        _u64(match.group("characteristic"), "native characteristic ID", positive=True)
        if (
            match.group("command_id"),
            match.group("connection_id"),
            match.group("gatt_id"),
        ) != ("12008", "12004", "12006"):
            raise RuntimeError("native read identity changed")

    if complete:
        if len(native_lines) != len(NATIVE_EVENTS) or any(
            len(entries) != 1 for entries in by_event.values()
        ):
            raise RuntimeError("expected exactly one of each native boundary marker")
    elif len(by_event["read-accepted"]) != 1:
        raise RuntimeError("expected exactly one native read-accepted marker")
    if any(len(entries) > 1 for entries in by_event.values()):
        raise RuntimeError("duplicate native boundary marker")

    if native_lines:
        probe_ids = {match.group("probe_id") for _, match in native_lines}
        identities = {
            (
                match.group("command_id"),
                match.group("connection_id"),
                match.group("gatt_id"),
                match.group("characteristic"),
            )
            for _, match in native_lines
        }
        if len(probe_ids) != 1:
            raise RuntimeError("native marker probe_id changed")
        if len(identities) != 1:
            raise RuntimeError("native read identity changed")
    return {event: entries[0] for event, entries in by_event.items() if entries}


def _exact(
    lines: list[str], consumed: set[int], expression: str
) -> tuple[int, Match[str]]:
    result = one(lines, expression)
    consumed.add(result[0])
    return result


def _optional_exact(
    lines: list[str], consumed: set[int], expression: str, label: str
) -> int | None:
    matches = [
        index for index, line in enumerate(lines) if re.fullmatch(expression, line)
    ]
    if len(matches) > 1:
        raise RuntimeError(f"duplicate {label}")
    if matches:
        consumed.add(matches[0])
        return matches[0]
    return None


def _reject_unconsumed_markers(lines: list[str], consumed: set[int]) -> None:
    """Reject malformed, unknown, or cross-generation markers fail closed."""
    unknown = [
        line
        for index, line in enumerate(lines)
        if index not in consumed and line.startswith(MODE + ":")
    ]
    if unknown:
        raise RuntimeError(f"unexpected RN ledger marker: {unknown[0]}")


def verify_ledger(
    text: str,
    *,
    run_id: str,
    post_steps: tuple[str, ...],
    old_characteristic: str | None = None,
    probe_id: str | None = None,
) -> tuple[str, str]:
    """Require two generations and exact ordered JS/native boundaries.

    ``post_steps`` is supplied by the controller from its confirmed host
    actions; it must prove exactly ``reload`` followed by ``arm``.  The return
    value is ``(old_characteristic_id, new_characteristic_id)``.
    """
    if post_steps != ("reload", "arm"):
        raise RuntimeError(
            "expected exactly two ordered, confirmed host notification posts"
        )
    lines = markers(text)
    consumed: set[int] = set()
    native = native_boundaries(lines, run_id=run_id, probe_id=probe_id, complete=True)
    consumed.update(index for index, _ in native.values())
    notification_registered = _optional_exact(
        lines,
        consumed,
        MODE + ":notification-registered",
        "notification registration marker",
    )
    generation_one_listener = _optional_exact(
        lines,
        consumed,
        MODE + ":generation=1:listener:registered-before-initialize",
        "generation 1 listener marker",
    )
    generation_one_initialize = _optional_exact(
        lines,
        consumed,
        MODE + ":generation=1:initialize:passed:commandId=12000",
        "generation 1 initialize marker",
    )
    generation_two_arm_listener = _optional_exact(
        lines,
        consumed,
        MODE + ":generation=2:arm-linking-listener:registered-before-initialize",
        "generation 2 arm-linking listener marker",
    )
    generation_two_initialize = _optional_exact(
        lines,
        consumed,
        MODE + ":generation=2:initialize:passed:commandId=13000",
        "generation 2 initialize marker",
    )
    arm_linking_dispatched = _optional_exact(
        lines,
        consumed,
        MODE + ":arm-linking-dispatched:accepted=true",
        "generation 2 arm-linking dispatch marker",
    )
    arm_js_observed = _optional_exact(
        lines,
        consumed,
        MODE + ":generation=2:arm-js-observed:source=linking:url=matched",
        "generation 2 arm JS marker",
    )

    old_scan, old_scanned = _exact(
        lines,
        consumed,
        rf"{MODE}:generation=1:scan:passed:serviceUuid={re.escape(SERVICE)}:"
        r"peripheralId=(0|[1-9][0-9]*):scanSessionId=12001",
    )
    old_connected, old_peer = _exact(
        lines,
        consumed,
        rf"{MODE}:generation=1:connected:passed:commandId=12005:"
        r"connectionId=12004:peripheralId=(0|[1-9][0-9]*)",
    )
    old_gatt, _ = _exact(
        lines,
        consumed,
        rf"{MODE}:generation=1:gatt:passed:commandId=12007:"
        r"connectionId=12004:gattSessionId=12006",
    )
    old_index, old = _exact(lines, consumed, MODE + ":" + OLD)
    ready, _ = _exact(lines, consumed, MODE + ":external-ready")
    notified, _ = _exact(lines, consumed, MODE + ":notification-received")
    cleanup, _ = _exact(lines, consumed, MODE + ":generation=1:cleanup-started")
    new_listener, _ = _exact(
        lines, consumed, MODE + ":generation=2:listener:registered-before-initialize"
    )
    new_scan, new_scanned = _exact(
        lines,
        consumed,
        rf"{MODE}:generation=2:scan:passed:serviceUuid={re.escape(SERVICE)}:"
        r"peripheralId=(0|[1-9][0-9]*):scanSessionId=13001",
    )
    new_connected, new_peer = _exact(
        lines,
        consumed,
        rf"{MODE}:generation=2:connected:passed:commandId=13005:"
        r"connectionId=13004:peripheralId=(0|[1-9][0-9]*)",
    )
    new_gatt, _ = _exact(
        lines,
        consumed,
        rf"{MODE}:generation=2:gatt:passed:commandId=13007:"
        r"connectionId=13004:gattSessionId=13006",
    )
    awaiting, awaited = _exact(lines, consumed, MODE + ":" + AWAITING_ARM)
    arm_ready, _ = _exact(lines, consumed, MODE + ":arm-ready")
    armed, _ = _exact(lines, consumed, MODE + ":arm-notification-received")
    dispatched, read_dispatch = _exact(lines, consumed, MODE + ":" + READ_DISPATCHED)
    new_index, new = _exact(lines, consumed, MODE + ":" + NEW)
    complete, _ = _exact(lines, consumed, MODE + ":complete:passed")

    first_char, old_peripheral = old.groups()
    second_char = new.group(1)
    _u64(old_scanned.group(1), "old peripheral ID")
    _u64(old_peer.group(1), "old connected peripheral ID")
    _u64(new_scanned.group(1), "new peripheral ID")
    _u64(new_peer.group(1), "new connected peripheral ID")
    _u64(first_char, "old characteristic ID", positive=True)
    _u64(second_char, "new characteristic ID", positive=True)
    if old_characteristic is not None and first_char != old_characteristic:
        raise RuntimeError("first read characteristic changed after WAITING")

    accepted_index, accepted = native["read-accepted"]
    disposed_index, disposed = native["dispose-returned"]
    late_index, late = native["late-submit-rejected"]
    native_characteristics = {
        match.group("characteristic") for match in (accepted, disposed, late)
    }
    if native_characteristics != {first_char}:
        raise RuntimeError("native read characteristic changed before old dispatch")

    if old_scanned.group(1) != old_peer.group(1) or old_peer.group(1) != old_peripheral:
        raise RuntimeError("first read did not use the scanned peripheral")
    if new_scanned.group(1) != new_peer.group(1):
        raise RuntimeError("second generation scan and connection do not correlate")
    if awaited.group(1) != second_char or read_dispatch.group(1) != second_char:
        raise RuntimeError(
            "generation 2 characteristic changed across the arm handshake"
        )

    if (
        not (
            old_scan
            < old_connected
            < old_gatt
            < accepted_index
            < old_index
            < ready
            < notified
            < cleanup
            < new_listener
            < new_scan
            < new_connected
            < new_gatt
            < awaiting
            < arm_ready
            < armed
            < dispatched
            < new_index
            < complete
        )
        or not cleanup < disposed_index < late_index < arm_ready
    ):
        raise RuntimeError(
            "reload, arm, read and completion boundaries are out of order"
        )

    if any(
        index > notified
        and line.startswith(MODE + ":generation=1:")
        and not line.endswith(":cleanup-started")
        for index, line in enumerate(lines)
    ):
        raise RuntimeError("late first-generation marker after reload")
    if notification_registered is not None and notification_registered >= old_scan:
        raise RuntimeError("notification registration marker is too late")
    if generation_one_listener is not None and generation_one_listener >= old_scan:
        raise RuntimeError("generation 1 listener marker is too late")
    if (
        notification_registered is not None
        and generation_one_listener is not None
        and notification_registered >= generation_one_listener
    ):
        raise RuntimeError("notification registration marker is out of order")
    if generation_one_initialize is not None and generation_one_initialize >= old_scan:
        raise RuntimeError("generation 1 initialize marker is too late")
    if generation_two_arm_listener is not None and not (
        late_index < generation_two_arm_listener < new_listener
    ):
        raise RuntimeError("generation 2 arm-linking listener marker is out of order")
    if generation_two_initialize is not None and not (
        new_listener < generation_two_initialize < new_scan
    ):
        raise RuntimeError("generation 2 initialize marker is out of order")
    if arm_linking_dispatched is not None and not (
        armed < arm_linking_dispatched < dispatched
    ):
        raise RuntimeError("generation 2 arm-linking dispatch marker is out of order")
    if arm_js_observed is not None and not (armed < arm_js_observed < dispatched):
        raise RuntimeError("generation 2 arm JS marker is out of order")
    if (
        arm_linking_dispatched is not None
        and arm_js_observed is not None
        and arm_linking_dispatched >= arm_js_observed
    ):
        raise RuntimeError("generation 2 arm JS marker is out of order")

    if any(":failed:" in line or ":close-rejected:" in line for line in lines):
        raise RuntimeError("RN reported failure")
    _reject_unconsumed_markers(lines, consumed)
    return first_char, second_char
