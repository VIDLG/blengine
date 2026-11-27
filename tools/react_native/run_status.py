"""The one node a UI scene polls: the workbench's `run-status` projection.

The App encodes it (`tests/consumers/react-native-bleng/src/runStatus.ts`), the host
decodes it here, and the native iOS scene driver matches the same pairs inside the test
process. All three state the same key set, because a scene polls far more often than it
acts and one element read is the only way its facts cannot disagree with each other.

`parse` is strict on purpose: a missing, unexpected, duplicated, empty or unparsable field
is drift, and drift must fail the run instead of silently stopping being checked. That is
why nothing here has a lenient "best effort" mode.

This module owns the contract, not a policy: callers decide what a decoded value means.
"""

from __future__ import annotations

import re

# Declared field order. The App writes the same order, but order is not part of the
# contract — `parse` compares sets, so a reordering is a change here, not a silent pass.
KEYS: tuple[str, ...] = (
    "state",
    "page",
    "scan",
    "link",
    "linkEnd",
    "gatt",
    "notify",
    "scanTotal",
    "notifyTotal",
    "notifyMode",
    "gattRead",
    "gattReads",
    "gattReadTotal",
    "scanTimeout",
    "scanFilter",
    "gattTarget",
    "gattMatch",
    "gattProps",
    # Link facts: each terminal is `none|pending|done|unsupported|failed`. `unsupported` is a
    # platform answer (CoreBluetooth has no MTU request, no priority, no cache invalidation),
    # so it is a first-class fact rather than a failure a scene has to guess at.
    "mtu",
    "mtuWrites",
    "mtuRequest",
    "priority",
    "refresh",
    # The CCCD as the peer stores it: a second witness for a subscription claim.
    "descriptor",
    "descriptorValue",
    "descriptorWrite",
    # Settled descriptor reads, monotonic. A scene advances it to prove a *fresh* read when
    # the expected bytes equal the value the previous read already left on screen.
    "descriptorReadTotal",
    # The system inventory across a live link: terminal, entry count, and whether this run's
    # peer appears. A count alone would not say the inventory describes *this* link.
    "devices",
    "devicesCount",
    "devicesPeer",
    "devicesKind",
    # The standing "connect when the peer appears" lane; `unsupported` is a platform answer.
    "auto",
    # Radio control: the terminal plus which direction was asked. `unsupported` is the expected
    # answer on the devices this project tests (API 33+ refuses adapter power changes; Apple has
    # no such API at all), so the scene asserts the refusal instead of a radio change.
    "radio",
    "radioTarget",
    "failure",
)

_FIELD = re.compile(r"([A-Za-z][A-Za-z0-9]*)=([^;]*)")
_COUNT = re.compile(r"[0-9]+")


class RunStatusError(ValueError):
    """The frame is not this projection."""


def parse(text: str) -> dict[str, str]:
    """Decode one frame, or raise `RunStatusError` naming the offending field."""
    if type(text) is not str:
        raise RunStatusError("run-status must be text")
    fields: dict[str, str] = {}
    for part in text.split(";"):
        match = _FIELD.fullmatch(part.strip())
        if match is None:
            raise RunStatusError(f"Invalid run-status field: {part[:80]!r}")
        key, value = match.groups()
        if key in fields:
            raise RunStatusError(f"Duplicate run-status field: {key!r}")
        if not value:
            raise RunStatusError(f"Empty run-status value: {key!r}")
        fields[key] = value
    if set(fields) != set(KEYS):
        raise RunStatusError(
            "run-status does not expose the declared fields: "
            f"missing {sorted(set(KEYS) - set(fields))!r}, "
            f"unexpected {sorted(set(fields) - set(KEYS))!r}"
        )
    return fields


def count(status: dict[str, str], key: str) -> int:
    """Read one count field, refusing a value that is not a count."""
    value = status[key]
    if _COUNT.fullmatch(value) is None:
        raise RunStatusError(f"run-status {key} is not a count: {value!r}")
    return int(value)


__all__ = ["KEYS", "RunStatusError", "count", "parse"]
