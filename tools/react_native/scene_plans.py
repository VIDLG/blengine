"""The declarative plan vocabulary both UI scene lanes run.

A plan is a list of steps over the App's *pinned machine surface*: element reads and taps on
identifiers the App publishes, bounded waits, and nothing else — no scroll, no coordinate and
no scenario decision. The scenario stays host-owned: fixture, physical lease, markers, cleanup
and the verdict never move into a driver.

The same plan is executed by either platform:

* iOS — `run_ios_scene_driver` drives it in-process with `XCUIElement`;
* Android — `run_android_scene_driver` drives it in-process with `UiDevice`.

Only the alert rules are data *about an environment* rather than about the App: `answerAlert`
declares the titles and buttons a driver may answer, and a driver answers only what it was
told (or records the alert as absent).

Step vocabulary: `wait`, `waitAny`, `still`, `tap`, `tapWhen`, `tapUntil`, `read`, `setText`,
`activate`, `answerAlert`, and `handoff`. A condition is a conjunction on one element:
`exists`, `label`, `labelContains`, `enabled`, `status`, `value`.

`handoff` is the one step the driver does not perform: it announces the named host action (a
fixture emission, a peer fault, a drop) and waits until the host confirms it finished. It
exists because a platform whose driver runs in the App's own process cannot carry BLE state
across separate runs, so the host's action has to happen while that process is alive. A single
process therefore runs every phase of a scene, pausing at each handoff, and the host still
performs — and is still guarded around — every action itself.

Read names are the receipt's keys, and a scene composes its phases from these builders, so a
name a host later consumes must be unique within one plan.
"""

from __future__ import annotations

import json
from typing import Any

# The App's own status projection (`src/runStatus.ts`) is the one node a scene polls.
STATUS_ID = "run-status"

# CCC enable/disable values, as the App writes them for one mode. Disable is the zero value in
# both modes: a disabled descriptor has no subscribed bit left to name. This module owns them
# because a *plan* is where those bytes are written; the scene derivation imports them.
CCC_BITS: dict[str, bytes] = {"notification": b"\x01\x00", "indication": b"\x02\x00"}

# Host-owned plans. A plan is only taps, reads and bounded waits; any scenario
# decision (which page, which value, whether a step is allowed) stays in the host.
#
# Every read is a `run-status` read, because that projection already carries the scalar
# facts a scene needs and its fields cannot disagree with each other. Nothing here touches
# scrollable content: the pinned surface is the scene's whole world.
#
# A plan is composed from the situation, not fixed: `fresh_install` decides whether the
# environmental prompts are looked for. A newly installed App asserts a local-network prompt
# at launch, then Bluetooth consent and "find Bluetooth devices" on the first scan, and all
# of them sit above the App in SpringBoard where they swallow taps. One step carries a rule
# per prompt it may answer and refuses anything it did not identify, so it can never dismiss
# an unrelated dialog.
ENVIRONMENTAL_ALERTS: dict[str, Any] = {
    "op": "answerAlert",
    "rules": [
        {
            "match": ["本地网络", "Local Network"],
            "buttons": ["允许", "Allow", "好", "OK"],
        },
        {
            "match": ["蓝牙", "Bluetooth"],
            "buttons": ["允许", "Allow", "好", "OK"],
        },
        # An Apple ID notice ("a device has been added to your account") is not a
        # permission: it has one acknowledgement button and grants nothing. It is declared
        # because it covers the App and swallows taps, and it re-posts until acknowledged.
        # The receipt records the exact title and button it answered, so this is auditable
        # rather than a silent dismissal; a plan may drop the rule by composing its own.
        {
            "match": [
                "你的账户中有设备加入",
                "device has been added to your account",
            ],
            "buttons": ["好", "OK"],
        },
    ],
    "timeout": 4.0,
}


def _alert_step(grace: float = 0.0) -> dict[str, Any]:
    """A fresh copy, so one plan's step is never shared by reference with another's.

    `grace` is how long this insertion point watches for a prompt it does not require. A
    prompt appears shortly after the action that causes it — measured once on a fresh install
    at 1.2 s (local network) and 1.6 s (Bluetooth consent) — so the sites that expect one
    declare 2 s, and the purely defensive ones declare 0.5 s to catch a prompt that is
    already rendering. `timeout` stays the bound for a `required` prompt.
    """
    step = json.loads(json.dumps(ENVIRONMENTAL_ALERTS))
    step["grace"] = grace
    return step


def _smoke(fresh_install: bool, attach: bool) -> tuple[dict[str, Any], ...]:
    steps: list[dict[str, Any]] = []
    if attach:
        # Foreground the App the host launched. It is a step, not a mode: a system prompt
        # must be answered first or activation fails and the scene never starts.
        if fresh_install:
            steps.append(_alert_step(0.5))
        steps.append({"op": "activate"})
    if fresh_install:
        steps.append(_alert_step(0.5))
    steps.append({"op": "wait", "id": "formal-host-title", "exists": True})
    steps.append({"op": "tap", "id": "start-session"})
    if fresh_install:
        steps.append(_alert_step(2.0))
    steps.append(
        {"op": "wait", "id": STATUS_ID, "status": {"state": "started"}, "timeout": 90}
    )
    steps += [
        {"op": "read", "id": STATUS_ID, "as": "started"},
        # Navigation is the only retryable action: it carries no BLE command. Each page
        # switch is asserted through the projection's own `page` field, so a click that
        # silently missed cannot pass.
        {"op": "tap", "id": "workbench-tests"},
        {"op": "wait", "id": STATUS_ID, "status": {"page": "Tests"}, "timeout": 20},
        {"op": "read", "id": STATUS_ID, "as": "testsPage"},
        {"op": "tap", "id": "workbench-current"},
        {
            "op": "wait",
            "id": STATUS_ID,
            "status": {"page": "Current run"},
            "timeout": 20,
        },
        {"op": "read", "id": STATUS_ID, "as": "currentPage"},
        {"op": "tap", "id": "workbench-evidence"},
        {"op": "wait", "id": STATUS_ID, "status": {"page": "Evidence"}, "timeout": 20},
        {"op": "read", "id": STATUS_ID, "as": "evidencePage"},
        {"op": "tap", "id": "workbench-current"},
        {
            "op": "wait",
            "id": STATUS_ID,
            "status": {"page": "Current run"},
            "timeout": 20,
        },
        {"op": "tap", "id": "close-session"},
        {"op": "wait", "id": STATUS_ID, "status": {"state": "closed"}, "timeout": 90},
        {"op": "read", "id": STATUS_ID, "as": "closed"},
    ]
    return tuple(steps)


# The App's own default observation budget, in milliseconds. A scene that needs a longer
# bound declares one; a scene that runs against the default requires the App to still be
# showing it, so a changed default fails instead of silently moving the bound.
DEFAULT_OBSERVATION_TIMEOUT_MS = 5000


def _prefix(
    fresh_install: bool,
    attach: bool,
    service_filter: str | None,
    observation_timeout_ms: int | None = None,
    declare_timeout: bool = False,
) -> list[dict[str, Any]]:
    """Everything a scene does before it starts touching the run.

    Attach mode foregrounds the App the host installed and pinned; a fresh installation
    answers the prompts iOS raises above it. The scan filter and the observation budget are
    declared inputs, so the scene proves the App took them instead of scanning for any
    nearby radio under whatever bound a human left in the input.
    """
    steps: list[dict[str, Any]] = []
    if attach:
        if fresh_install:
            steps.append(_alert_step(0.5))
        steps.append({"op": "activate"})
    if fresh_install:
        steps.append(_alert_step(0.5))
    steps.append({"op": "wait", "id": "formal-host-title", "exists": True})
    # The scene's precondition is a real observation, not an assumption.
    steps.append({"op": "read", "id": STATUS_ID, "as": "initial"})
    steps.append({"op": "tap", "id": "start-session"})
    if fresh_install:
        steps.append(_alert_step(2.0))
    steps += [
        {"op": "wait", "id": STATUS_ID, "status": {"state": "started"}, "timeout": 90},
        {"op": "read", "id": STATUS_ID, "as": "started"},
    ]
    if observation_timeout_ms is not None:
        # The budget is part of the scene's declared request, like the service filter: a
        # scene that needs a different bound than the App's default states it, and one that
        # runs against the default requires the App to still report it. Both happen before
        # the scan that will honour it.
        declared = str(observation_timeout_ms)
        if declare_timeout:
            steps.append(
                {
                    "op": "setText",
                    "id": "scan-observation-timeout",
                    "text": declared,
                    "timeout": 30,
                }
            )
        steps.append(
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"scanTimeout": declared},
                "timeout": 20,
            }
        )
    if service_filter is not None:
        # The console (and so this input) is the Current run page's pinned surface, which
        # the session start selects. The filter is a scan parameter, so it is declared
        # here — after the page exists and before the scan that must honour it.
        steps.append(
            {
                "op": "setText",
                "id": "scan-service-filter",
                "text": service_filter,
                "timeout": 30,
            }
        )
        steps.append(
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"scanFilter": service_filter},
                "timeout": 20,
            }
        )
    return steps


def _scan_stop(
    fresh_install: bool, expected_local_name: str | None
) -> list[dict[str, Any]]:
    """Start the scan, match the declared peer, stop it."""
    steps: list[dict[str, Any]] = [{"op": "tap", "id": "start-scan"}]
    if fresh_install:
        # The first scan is where iOS asks to "find Bluetooth devices" on top of the use-
        # Bluetooth consent, and a tap on a covered screen reports a timeout, not an alert.
        steps.append(_alert_step(2.0))
    steps += [
        {
            "op": "wait",
            "id": STATUS_ID,
            "status": {"scan": "running", "failure": "none"},
            "timeout": 60,
        },
        {
            "op": "tapUntil",
            "id": "next-scan-event",
            "until": {
                "id": "scan-latest",
                "labelContains": f'name="{expected_local_name}"'
                if expected_local_name
                else "name=",
            },
            "maxTaps": 40,
            "timeout": 180,
        },
        {"op": "read", "id": STATUS_ID, "as": "scanning"},
        {"op": "tap", "id": "stop-scan"},
        {
            "op": "wait",
            "id": STATUS_ID,
            "status": {"scan": "stopped", "failure": "none"},
            "timeout": 60,
        },
        {"op": "read", "id": STATUS_ID, "as": "stopped"},
    ]
    return steps


def _close() -> list[dict[str, Any]]:
    return [
        {"op": "tap", "id": "close-session"},
        {"op": "wait", "id": STATUS_ID, "status": {"state": "closed"}, "timeout": 90},
        {"op": "read", "id": "action-ledger", "as": "ledger"},
        {"op": "read", "id": STATUS_ID, "as": "closed"},
    ]


def _scan(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None = None,
    service_filter: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Find the declared peer and stop scanning: the shortest complete scene."""
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _close()
    )


def _subscribe(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
    expected_mode: str,
) -> tuple[dict[str, Any], ...]:
    """Everything up to an established subscription; the host emits after this.

    The phase boundary is a fact of the firmware, not a style: a notification can only be
    delivered once the peer's CCC is enabled, so the host has to act between two runs of
    the App. This plan therefore stops at `notify=subscribed` and never closes the session.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + [
            {"op": "tap", "id": "connect-peripheral"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"link": "connected"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "linked"},
            {"op": "tap", "id": "discover-gatt"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gatt": "ready"},
                "timeout": 60,
            },
            # The target is declared by identity *after* discovery, because resolution is an
            # observation over the discovered topology: the App says whether exactly one
            # row matched it.
            {
                "op": "setText",
                "id": "gatt-target",
                "text": characteristic_uuid,
                "timeout": 30,
            },
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattTarget": characteristic_uuid, "gattMatch": "one"},
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "topology"},
            {"op": "tap", "id": "gatt-subscribe"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "subscribed", "notifyMode": expected_mode},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "subscribed"},
        ]
    )


def _pull_value(value_hint: str | None) -> list[dict[str, Any]]:
    """Ask the App for the value the host emitted, and read the frame that carries it.

    The pull is one *blocking* observation: the App's `nextEvent(timeout)` returns as soon as a
    value arrives. Tapping a second time while the first value is still rendering would find an
    empty queue and record a failed pull, which is a real failure and not a retry, so the loop
    that used to exist here is gone.
    """
    return [
        {"op": "tap", "id": "pull-notification"},
        {
            "op": "wait",
            "id": "notify-latest",
            "labelContains": value_hint if value_hint else "bytes",
            "timeout": 90,
        },
        {"op": "read", "id": STATUS_ID, "as": "notified"},
    ]


def _collect(value_hint: str | None) -> tuple[dict[str, Any], ...]:
    """Pull the value the host emitted, then disable the subscription and close."""
    return tuple(
        _pull_value(value_hint)
        + [
            {"op": "tap", "id": "disable-notifications"},
            _await_recorded("notify-disable"),
            # The App's own record is what says whether the disable settled: one phase later
            # every derivation reads it again, but a scene that stops here must still carry
            # the fact that explains why.
            {"op": "read", "id": "action-ledger", "as": "after-disable"},
            # A successful disable *is* the stream's terminal, so the App publishes it as soon
            # as the peer acknowledges. The scene only has to observe it; pulling again would
            # ask a closed lane for a value.
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "ended"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "ended"},
        ]
        + _close()
    )


# One step and no BLE command: it exists so a device can be inspected without driving a
# scene. The trees it attaches answer "is something covering the App?" without a screenshot.
PROBE_PLAN: tuple[dict[str, Any], ...] = (
    {"op": "wait", "id": STATUS_ID, "exists": True},
)

# The App clears `gatt-result` when a command is dispatched and re-renders it only once that
# command settled, which is what makes two reads in one scene distinguishable: a value read
# waits for the *value* rendering, never for the phase (`gattRead` is still `done` from the
# first read) and never for mere presence (the write receipt is present until the tap).
VALUE_RENDERING = "bytes ["
WRITE_RENDERING = " wrote "
# The cancellation scene's declared observation budget on the App's own input. The queued
# read waits behind a *held* one, so the App's default 5 s would expire before the cancel
# this scene exists to prove; it is declared, not inherited, and stays inside the firmware's
# own hold budget so a stuck hold fails instead of hanging.
CANCELLATION_TIMEOUT_MS = 30000
# How long the App is actually away in the background scene. A background leave that lasts only
# as long as one host action proves nothing: an App that was never suspended cannot lose
# anything. iOS suspends a backgrounded App within a few seconds, so a window this size makes
# "the subscription outlived the background" a fact the platform had every chance to refute.
# It is declared here, in the plan, because it is scene policy — and the driver refuses a
# `background` step that does not state its window (convention C8).
BACKGROUND_DWELL_SECONDS = 30


def _resume() -> list[dict[str, Any]]:
    """Re-enter a scene whose App is already the pinned one.

    A multi-phase scene's later phases are separate test processes against the same pid. The
    App keeps its run, so a later phase only has to prove it is foreground and still the
    session it left behind — no session, scan, link or discovery command is repeated, and
    none is inferred from the previous phase's receipt.
    """
    return [
        {"op": "activate"},
        {"op": "wait", "id": STATUS_ID, "status": {"state": "started"}, "timeout": 30},
    ]


# The read/write scenes submit, in order: one held read (settles when the peer releases it),
# one queued read (settles as cancelled), one held read that completes, and — for
# cancellation — one recovery read. A settled-op count is the only fact that tells two
# sequential reads apart, because the App keeps showing the previous read's value and keeps
# `gattRead=done` between them.
CANCELLATION_READS = 3


def _declare_target(
    characteristic_uuid: str, *, type_it: bool = True
) -> list[dict[str, Any]]:
    """Link, discover, then declare the characteristic by identity.

    Resolution is an observation over the discovered topology, so the target is stated
    after discovery — before it, the App has nothing to resolve against.

    `type_it=False` is for a later cycle in the *same* App: the input still holds the value
    the previous cycle typed, so typing it again would be a needless mutation (and a
    controlled input is the field whose replacement can race its own re-render). The wait
    afterwards still proves the retained value resolves to exactly one row.
    """
    declare: list[dict[str, Any]] = (
        [
            {
                "op": "setText",
                "id": "gatt-target",
                "text": characteristic_uuid,
                "timeout": 30,
            }
        ]
        if type_it
        else []
    )
    return [
        {"op": "tap", "id": "connect-peripheral"},
        {"op": "wait", "id": STATUS_ID, "status": {"link": "connected"}, "timeout": 60},
        {"op": "read", "id": STATUS_ID, "as": "linked"},
        {"op": "tap", "id": "discover-gatt"},
        {"op": "wait", "id": STATUS_ID, "status": {"gatt": "ready"}, "timeout": 60},
        *declare,
        {
            "op": "wait",
            "id": STATUS_ID,
            "status": {"gattTarget": characteristic_uuid, "gattMatch": "one"},
            "timeout": 30,
        },
        {"op": "read", "id": STATUS_ID, "as": "topology"},
    ]


def _cancellation_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
    observation_timeout_ms: int,
) -> tuple[dict[str, Any], ...]:
    """Open the live link and declare the target the host will hold a read on.

    This is its own phase because the fixture will not arm a fault before it has a
    connected peer: the hold can only be placed on a link that already exists.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            observation_timeout_ms,
            declare_timeout=True,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
    )


def _cancellation_hold() -> tuple[dict[str, Any], ...]:
    """Submit the read the peer is already holding.

    The host armed the hold before this phase starts, so the submission must stay
    outstanding: a `done` here would mean the peer was not holding anything and the queue
    this scene exists to prove never existed.
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "waiting", "gattReads": "1"},
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "held"},
        ]
    )


def _cancellation_queue(characteristic_uuid: str) -> tuple[dict[str, Any], ...]:
    """Queue a second read behind the held one, then cancel exactly that one.

    `gattReads=2` is the queue itself, not a claim about either read; cancelling pops the
    newest, so the peer-held read is left alone and `gattReads` returns to 1. A cancelled
    read renders no value, and its own cancellation terminal is a pinned element.
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattReads": "2"},
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "queued"},
            {"op": "tap", "id": "cancel-gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "cancelled", "gattReads": "1"},
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "cancelled"},
            # The cancelled read must not have rendered a value. Nothing can have finished
            # here: the peer still holds the older read, and the cancelled one never will.
            {"op": "wait", "id": "gatt-result", "exists": False, "timeout": 10},
            {"op": "wait", "id": "gatt-cancel", "exists": True, "timeout": 30},
            {"op": "read", "id": "gatt-cancel", "as": "cancel"},
        ]
    )


def _cancellation_settle() -> tuple[dict[str, Any], ...]:
    """After the peer releases the hold: the first read completes, a fresh one works.

    The recovery read waits for the settled count, never for the value: the App still shows
    the completed read's rendering, so `bytes [` would pass on stale text. Observing the
    advanced count implies the new value is rendered too, because the App publishes the
    outcome before it counts it.
    """
    return tuple(
        _resume()
        + [
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {
                    "gattRead": "done",
                    "gattReads": "0",
                    "gattReadTotal": str(CANCELLATION_READS - 1),
                },
                "timeout": 60,
            },
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "completed"},
            {"op": "read", "id": "gatt-result", "as": "completed-value"},
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {
                    "gattRead": "done",
                    "gattReadTotal": str(CANCELLATION_READS),
                },
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "recovered"},
            {"op": "read", "id": "gatt-result", "as": "recovered-value"},
        ]
        + _close()
    )


# The write-reject scene submits three reads and two writes: the read a refusal must leave
# untouched, the read after the refusal, and the read after the write that succeeds.
WRITE_REJECT_READS = 3


def _write_reject_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
    write_text: str,
) -> tuple[dict[str, Any], ...]:
    """Open the link, state the value, then declare the payload — all before any hold.

    The initial read is the value a refusal must leave untouched, so it has to happen
    before the peer parks anything. Typing the payload is UI work and belongs outside the
    firmware's hold window too.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
        + [
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "done", "gattReadTotal": "1"},
                "timeout": 60,
            },
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 30,
            },
            # The value read is namespaced: `_prefix` already publishes the session
            # precondition as `initial`, and a receipt is one flat name -> text map.
            {"op": "read", "id": "gatt-result", "as": "initial-value"},
            {
                "op": "setText",
                "id": "gatt-write-value",
                "text": write_text,
                "timeout": 30,
            },
        ]
    )


def _write_reject_dispatch() -> tuple[dict[str, Any], ...]:
    """Submit the write the peer is already holding.

    Nothing is published while a write waits, so the App's own ledger is what says the
    submission was accepted and its wait is outstanding; the hold is the host's half and
    the peer's refusal is what settles it.
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "gatt-write"},
            _await_recorded("gatt-write"),
            {"op": "read", "id": "action-ledger", "as": "ledger"},
        ]
    )


def _write_reject_settle() -> tuple[dict[str, Any], ...]:
    """The refusal, an unchanged read, then the same write with no hold at all.

    Each read is attributed by the settled count, because the App keeps the previous
    rendering between reads; the refusal's own rendering is the pinned error row, and a
    refused write must not have rendered a receipt.
    """
    return tuple(
        _resume()
        + [
            {"op": "wait", "id": "gatt-error", "exists": True, "timeout": 60},
            {"op": "read", "id": "gatt-error", "as": "rejection"},
            {"op": "wait", "id": "gatt-result", "exists": False, "timeout": 10},
            {"op": "read", "id": STATUS_ID, "as": "rejected"},
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "done", "gattReadTotal": "2"},
                "timeout": 60,
            },
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 30,
            },
            {"op": "read", "id": "gatt-result", "as": "unchanged"},
            {"op": "tap", "id": "gatt-write"},
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": WRITE_RENDERING,
                "timeout": 60,
            },
            {"op": "read", "id": "gatt-result", "as": "recovered"},
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "done", "gattReadTotal": "3"},
                "timeout": 60,
            },
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 30,
            },
            {"op": "read", "id": "gatt-result", "as": "stored"},
        ]
        + _close()
    )


# The CCC scenes act on the same characteristic a subscription scene does, so they take the
# same profile-derived target (`uuid`, `mode`, `value`); the mode is also what the CCC bytes
# the peer parks are derived from.
def _ccc_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
) -> tuple[dict[str, Any], ...]:
    """Open the link and declare the target whose CCC a subscription writes."""
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
            declare_timeout=True,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
    )


def _await_recorded(action: str) -> dict[str, Any]:
    """Wait until the App's own ledger carries the wait row for [action].

    A tap only injects a touch. On a platform whose automation returns as soon as the touch
    lands (`UiAutomator`), the ledger read that follows can win the race against the App's
    own handler and describe the ledger from before the tap — observed once on a device: the
    same plan and the same steps, one run ending the disable with two statements and one
    without them.

    The handler records `<action>`, settles it, then records `<action>-wait` — one
    synchronous block, before it awaits anything — so the wait row's presence is what says
    the handler has run. Its outcome is not constrained (`pending` while the peer holds the
    submission, `succeeded` when it landed), because the same settlement step serves both
    held and instant scenes; the derivation still asserts the exact tail, and a ledger that
    never carries this row fails here naming the element and the missing text.
    """
    if type(action) is not str or not action.strip():
        raise ValueError("a recorded action needs its own name")
    return {
        "op": "wait",
        "id": "action-ledger",
        "labelContains": f"{action}-wait|",
        "timeout": 30,
    }


def _ccc_enable_hold() -> tuple[dict[str, Any], ...]:
    """Subscribe while the peer holds the CCC write.

    The App's own ledger is the witness: its handler records `notify-enable` and then
    `notify-enable-wait` in one synchronous block *before* it awaits anything, and a `pending`
    wait is exactly "accepted, still outstanding". The status is read but not required to
    equal the transient `enabling`: on 2026-11-26 one press took 120.6 s while `hittable`
    stayed at 0.1 s, so the read landed after the state had already settled and the scene
    failed on "notify=enabling within 30 s" although its real fact was still there.
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "gatt-subscribe"},
            _await_recorded("notify-enable"),
            {"op": "read", "id": "action-ledger", "as": "ledger"},
            {"op": "read", "id": STATUS_ID, "as": "held"},
        ]
    )


def _ccc_enable_settle(expected_mode: str) -> tuple[dict[str, Any], ...]:
    """The refused enable is a *failed* subscription; a retry establishes and reports it."""
    return tuple(
        _resume()
        + [
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "failed"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "rejected"},
            {"op": "tap", "id": "gatt-subscribe"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "subscribed", "notifyMode": expected_mode},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "recovered"},
        ]
    )


def _ccc_disable_hold() -> tuple[dict[str, Any], ...]:
    """Disable while the peer holds the CCC write.

    A parked disable publishes nothing new — the subscription still looks established until
    the write settles — so the ledger is again the only fact that says "accepted, waiting".
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "disable-notifications"},
            _await_recorded("notify-disable"),
            {"op": "read", "id": "action-ledger", "as": "ledger"},
        ]
    )


def _ccc_disable_probe() -> tuple[dict[str, Any], ...]:
    """The refusal, one probe of delivery, then a real disable and the stream's terminal.

    A refused CCC write leaves the peer enabled, so a value is still possible; a platform may
    also have stopped delivering locally. Both answers are legitimate — the disjunction
    reports which one happened, and the host holds the App to its own account of it. A value
    arriving is `subscribed`; a pull that settles no value is a `notify-pull|failed` line in
    the App's ledger (never a claimed end).

    The second disable is tapped once and then *observed*, never poked again: a disable that
    settles withdraws the pull control on purpose, so a plan that taps until something happens
    would be asking a closed lane for a value and calling its own retry a failure. The App's
    own disable wait is bounded by the declared observation timeout, so the settle window is
    declared too, and the derivation — not a step timeout — says what settled.
    """
    return tuple(
        _resume()
        + [
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "unverified"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "rejected"},
            {"op": "tap", "id": "pull-notification"},
            {
                "op": "waitAny",
                "any": [
                    {"id": STATUS_ID, "status": {"notify": "subscribed"}},
                    {"id": "action-ledger", "labelContains": "notify-pull|failed|"},
                ],
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "probed"},
            {"op": "tap", "id": "disable-notifications"},
            # The settle budget, declared: the App's own disable wait is capped by the
            # observation timeout on screen (5 s by default), so a twice-refused disable and
            # an accepted one both settle well inside it. Every step after this one still
            # runs, so the receipt carries both facts either way.
            {"op": "still", "id": STATUS_ID, "exists": True, "for": 8, "timeout": 30},
            # What the App recorded for this second disable is the fact that explains the
            # phase it then publishes; without it a refused-again disable looks identical to
            # a disable that never left.
            {"op": "read", "id": "action-ledger", "as": "after-disable"},
            {"op": "read", "id": STATUS_ID, "as": "ended"},
        ]
        + _close()
    )


# The link's quiet window: the App's own default silence claim (5 s) with a second of margin,
# measured by `still` rather than slept through.
PASSIVE_SILENCE_SECONDS = 6
# Every control that needs a live link. A link the App reports as ended must not leave any of
# them offered.
CONTROLS_REQUIRING_LINK = (
    "gatt-read",
    "gatt-write",
    "discover-gatt",
    "read-rssi",
    "gatt-subscribe",
    "pull-notification",
    "disconnect-peripheral",
)


def _passive_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
) -> tuple[dict[str, Any], ...]:
    """Prove the link works before its end can mean anything."""
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
        + [
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "done", "gattReadTotal": "1"},
                "timeout": 60,
            },
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 30,
            },
            {"op": "read", "id": "gatt-result", "as": "initial-value"},
        ]
    )


def _passive_ended() -> tuple[dict[str, Any], ...]:
    """The App announces the end by itself, then nothing changes for the quiet window.

    Nothing is tapped before the announced end is read: a command would measure a failing
    command instead of an observation. The rendered value is on screen too, so the window
    requires it to be the same one, and every control that needs a link must be withdrawn.
    """
    return tuple(
        _resume()
        + [
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"link": "disconnected", "linkEnd": "unrequested"},
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "announced"},
            {"op": "read", "id": "gatt-result", "as": "after"},
            {
                "op": "still",
                "id": STATUS_ID,
                "status": {"link": "disconnected", "linkEnd": "unrequested"},
                "for": PASSIVE_SILENCE_SECONDS,
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "silent"},
        ]
        + [
            {"op": "wait", "id": identifier, "enabled": False, "timeout": 15}
            for identifier in CONTROLS_REQUIRING_LINK
        ]
        + _close()
    )


# The connect-failure scene's declared observation budget on the App's own input. The platform
# needs longer than the App's 5 s default to answer a connect nobody can complete, and the
# *outstanding native attempt* is the point: a shorter wait ends the scene while the platform is
# still initiating, and that leftover initiator connects to the peer as soon as a later scene
# starts advertising. A 30 s budget lets the platform report its own failure and settle.
CONNECT_FAILURE_TIMEOUT_MS = 30000


def _connect_failure_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
) -> tuple[dict[str, Any], ...]:
    """Reach the peer and stop: the advertisement is the address the next run will use.

    No connect is attempted here. The host has to withdraw the peer *after* the scan matched
    — before it, there is no advertisement for the App to address — so this phase states only
    that the peer was found, and declares the budget the attempt will be allowed to spend.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            CONNECT_FAILURE_TIMEOUT_MS,
            declare_timeout=True,
        )
        + _scan_stop(fresh_install, expected_local_name)
    )


def _connect_failure_attempt() -> tuple[dict[str, Any], ...]:
    """Submit the connect the withdrawn peer cannot answer, then hold the App's account.

    The wait is on the App's *own record* (`connect-wait|failed|`), because the projection's
    `link` was already `none` before the tap: only the settled wait row says the attempt
    finished rather than never started. The quiet window then holds "no link, no end" for
    longer than a platform connect can invent one.
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "connect-peripheral"},
            {
                "op": "wait",
                "id": "action-ledger",
                "labelContains": "connect-wait|failed|",
                "timeout": 150,
            },
            {"op": "read", "id": STATUS_ID, "as": "refused"},
            {
                "op": "still",
                "id": STATUS_ID,
                "status": {"link": "none", "linkEnd": "none"},
                "for": PASSIVE_SILENCE_SECONDS,
                "timeout": 40,
            },
            {"op": "read", "id": STATUS_ID, "as": "silent"},
        ]
        + [
            {"op": "wait", "id": identifier, "enabled": False, "timeout": 15}
            for identifier in CONTROLS_REQUIRING_LINK
        ]
        + _close()
    )


def _answered(key: str, timeout: float = 90) -> dict[str, Any]:
    """One link fact's terminal, in either of its two honest shapes.

    `done` is the platform did it; `unsupported` is the platform said it has no such
    capability. Both are answers a scene may accept, and neither may be reached through
    `failed` or a timeout — which is why the plan names exactly these two.
    """
    return {
        "op": "waitAny",
        "any": [
            {"id": STATUS_ID, "status": {key: "done"}},
            {"id": STATUS_ID, "status": {key: "unsupported"}},
        ],
        "timeout": timeout,
    }


def _link_facts(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
) -> tuple[dict[str, Any], ...]:
    """Query then request the MTU, ask for priority, and invalidate the cache last.

    No characteristic is declared: every one of these is a fact about the link, answered by
    the platform. The cache refresh is last on purpose — invalidating the platform's topology
    can retire the handles a read or a subscription would need, so nothing may follow it.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + [
            {"op": "tap", "id": "connect-peripheral"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"link": "connected"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "linked"},
            {"op": "tap", "id": "mtu-query"},
            {"op": "wait", "id": STATUS_ID, "status": {"mtu": "done"}, "timeout": 90},
            {"op": "read", "id": STATUS_ID, "as": "mtu"},
            {"op": "tap", "id": "mtu-request"},
            _answered("mtuRequest"),
            {"op": "read", "id": STATUS_ID, "as": "mtuRequest"},
            # Ask again: an accepted request is a receipt, not an effect. Re-reading the same
            # fact is what makes "the platform applied it" observable instead of assumed.
            {"op": "tap", "id": "mtu-query"},
            {"op": "wait", "id": STATUS_ID, "status": {"mtu": "done"}, "timeout": 90},
            {"op": "read", "id": STATUS_ID, "as": "mtuAfter"},
            {"op": "tap", "id": "priority-high"},
            _answered("priority"),
            {"op": "read", "id": STATUS_ID, "as": "priority"},
            {"op": "tap", "id": "refresh-gatt"},
            _answered("refresh"),
            {"op": "read", "id": STATUS_ID, "as": "refresh"},
        ]
        + _close()
    )


def _adapter_control(
    fresh_install: bool,
    attach: bool,
    service_filter: str | None,
) -> tuple[dict[str, Any], ...]:
    """Ask the platform to change the radio's power state, both directions.

    No peer, no scan and no link: radio power is an adapter fact. On every device this project
    owns the answer is the platform's own refusal, and that is what makes the scene worth
    running — Android's `BluetoothAdapter` note says an app targeting API 33+ cannot change the
    radio unless it is a device/profile owner or a system app (the call always returns false),
    and CoreBluetooth exposes no such API. A device that is exempt would answer `done`, which
    the plan also accepts: what must hold either way is that the App reports the platform's own
    answer, in the direction it asked for, and claims nothing else.
    """

    def answered(direction: str) -> list[dict[str, Any]]:
        return [
            {"op": "tap", "id": f"radio-{direction}"},
            {
                "op": "waitAny",
                "any": [
                    {
                        "id": STATUS_ID,
                        "status": {"radio": "done", "radioTarget": direction},
                    },
                    {
                        "id": STATUS_ID,
                        "status": {"radio": "unsupported", "radioTarget": direction},
                    },
                ],
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": direction},
        ]

    return tuple(
        _prefix(fresh_install, attach, service_filter, DEFAULT_OBSERVATION_TIMEOUT_MS)
        + [
            # The probe surface is its own page; the scene states it instead of assuming it.
            {"op": "tap", "id": "workbench-probe"},
            {"op": "wait", "id": "workbench-page", "label": "Probe", "timeout": 30},
        ]
        # Both directions, in this order: a refusal must not leave the App believing the radio
        # is off, and the "on" request is what a device that really turned it off needs to
        # restore. The second answer also proves the two requests stay apart.
        + answered("off")
        + answered("on")
        + _close()
    )


def _devices_kinds(
    fresh_install: bool,
    attach: bool,
    service_filter: str | None,
) -> tuple[dict[str, Any], ...]:
    """Ask for the two inventories the observation main line never needs.

    No peer, no scan and no link: `bonded` and `companion` are adapter facts, so the scene
    proves only that each answer is the platform's own — an inventory, or its refusal.
    """

    def answered(kind: str) -> list[dict[str, Any]]:
        return [
            {"op": "tap", "id": f"devices-{kind}"},
            {
                "op": "waitAny",
                "any": [
                    {
                        "id": STATUS_ID,
                        "status": {"devices": "done", "devicesKind": kind},
                    },
                    {
                        "id": STATUS_ID,
                        "status": {"devices": "unsupported", "devicesKind": kind},
                    },
                ],
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": kind},
        ]

    return tuple(
        _prefix(fresh_install, attach, service_filter, DEFAULT_OBSERVATION_TIMEOUT_MS)
        + [
            # The probe surface is its own page; the scene states it instead of assuming it.
            {"op": "tap", "id": "workbench-probe"},
            {"op": "wait", "id": "workbench-page", "label": "Probe", "timeout": 30},
        ]
        + answered("bonded")
        + answered("companion")
        + _close()
    )


# The standing request needs more than the App's 5 s default: the platform must stay willing
# to connect across the peer's absence, so the scene declares a budget a plain connect would
# never need. A timeout here would be the scene's own bug, not the platform's answer.
AUTO_CONNECT_TIMEOUT_MS = 60000


def _auto_connect_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
) -> tuple[dict[str, Any], ...]:
    """Find the peer and stop: the advertisement is the address the standing request uses."""
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            AUTO_CONNECT_TIMEOUT_MS,
            declare_timeout=True,
        )
        + _scan_stop(fresh_install, expected_local_name)
    )


def _auto_connect_arm() -> tuple[dict[str, Any], ...]:
    """Ask for a link while the peer is away; the request must outlive the absence.

    A platform without the capability answers immediately, so the request step accepts that
    answer too — the settle phase is what states which answer the scene will accept.
    """
    return tuple(
        _resume()
        + [
            # The probe surface lives on its own page. The scene states that instead of
            # assuming it, so moving a capability between pages fails here by name.
            {"op": "tap", "id": "workbench-probe"},
            {"op": "wait", "id": "workbench-page", "label": "Probe", "timeout": 30},
            {"op": "tap", "id": "auto-connect"},
            {
                "op": "waitAny",
                "any": [
                    {"id": STATUS_ID, "status": {"auto": "pending"}},
                    {"id": STATUS_ID, "status": {"auto": "unsupported"}},
                ],
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "submitted"},
        ]
    )


def _auto_connect_settle() -> tuple[dict[str, Any], ...]:
    """The platform's answer after the peer returns, then the run's own record."""
    return tuple(
        _resume()
        + [
            {
                "op": "waitAny",
                "any": [
                    {"id": STATUS_ID, "status": {"auto": "done", "link": "connected"}},
                    {"id": STATUS_ID, "status": {"auto": "unsupported"}},
                ],
                "timeout": 240,
            },
            {"op": "read", "id": STATUS_ID, "as": "auto"},
        ]
        + _close()
    )


def _devices_query(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
) -> tuple[dict[str, Any], ...]:
    """Read the platform's own `connected` inventory and find this run's peer in it.

    No characteristic is declared: the inventory is a fact about the adapter, not about the
    GATT topology. The link is what makes the claim meaningful — an inventory that does not
    contain the peer this App is holding describes something else.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + [
            {"op": "tap", "id": "connect-peripheral"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"link": "connected"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "linked"},
            {"op": "tap", "id": "devices-query"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"devices": "done"},
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "devices"},
        ]
        + _close()
    )


def _descriptor_io(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
    expected_mode: str,
) -> tuple[dict[str, Any], ...]:
    """Read the peer's CCCD across a subscription's whole life: off, on, cleared, written.

    Every read is its own command, so a scene can assert the peer's answer at each point
    instead of inferring it from the App's phase. The write sends back exactly the bytes the
    preceding read reported: the same value is the one write a workbench can make without
    changing the subscription state the lane already owns.

    Each read declares `advance: descriptorReadTotal`, so "the value I expect" can never be
    satisfied by the *previous* read's value: the step only settles once the App has settled
    one more read than when it started. Without that, the final "unchanged" read would pass
    on the value the disabled read already left on screen, without ever touching the peer.
    """
    enabled = CCC_BITS[expected_mode].hex()
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
        + [
            {
                "op": "tapUntil",
                "id": "descriptor-read",
                "until": {
                    "id": STATUS_ID,
                    "status": {"descriptorValue": "0000"},
                    "exists": True,
                },
                "advance": "descriptorReadTotal",
                "maxTaps": 5,
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "cccd-initial"},
            {"op": "tap", "id": "gatt-subscribe"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "subscribed", "notifyMode": expected_mode},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "subscribed"},
            # The first read after a state change can still observe the previous value on a
            # stack that serves the descriptor from its own cache; the plan states the
            # tolerance (at most five re-reads) instead of hiding a retry in the App.
            {
                "op": "tapUntil",
                "id": "descriptor-read",
                "until": {
                    "id": STATUS_ID,
                    "status": {"descriptorValue": enabled},
                    "exists": True,
                },
                "advance": "descriptorReadTotal",
                "maxTaps": 5,
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "cccd-enabled"},
            # Disable first and read the peer *before* any direct descriptor write: only then
            # can "the peer cleared it" be attributed to the disable rather than to our write.
            {"op": "tap", "id": "disable-notifications"},
            _await_recorded("notify-disable"),
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "ended"},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "ended"},
            {
                "op": "tapUntil",
                "id": "descriptor-read",
                "until": {
                    "id": STATUS_ID,
                    "status": {"descriptorValue": "0000"},
                    "exists": True,
                },
                "advance": "descriptorReadTotal",
                "maxTaps": 5,
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "cccd-cleared"},
            {"op": "tap", "id": "descriptor-write"},
            # A platform may refuse a direct CCCD write as itself (CoreBluetooth requires
            # `setNotifyValue(_:for:)`), so the scene accepts either answer and the derivation
            # proves the bytes are unchanged either way.
            {
                "op": "waitAny",
                "any": [
                    {"id": STATUS_ID, "status": {"descriptorWrite": "done"}},
                    {"id": STATUS_ID, "status": {"descriptorWrite": "unsupported"}},
                ],
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "cccd-written"},
            # A fresh read, not the value the cleared read left behind: `advance` is what
            # forces this tap even though the expected value already equals the current one.
            {
                "op": "tapUntil",
                "id": "descriptor-read",
                "until": {
                    "id": STATUS_ID,
                    "status": {"descriptorValue": "0000"},
                    "exists": True,
                },
                "advance": "descriptorReadTotal",
                "maxTaps": 5,
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "cccd-unchanged"},
        ]
        + _close()
    )


def _disconnect_open(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
    expected_mode: str,
) -> tuple[dict[str, Any], ...]:
    """Prove the link carries a read and a subscription before the operator ends it.

    The subscription is what makes a later `ended` phase meaningful: an App that never
    subscribed has nothing to retire, and a phase that keeps claiming `subscribed` on a dead
    link would pass such a scene for the wrong reason.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
        + [
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 60,
            },
            {"op": "read", "id": "gatt-result", "as": "disconnect-initial"},
            {"op": "tap", "id": "gatt-subscribe"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"notify": "subscribed", "notifyMode": expected_mode},
                "timeout": 60,
            },
            {"op": "read", "id": STATUS_ID, "as": "subscribed"},
        ]
    )


def _disconnect_sever() -> tuple[dict[str, Any], ...]:
    """The operator's own end, then the same facts held over a quiet window.

    Nothing here needs the link: the tap is the end itself, and everything after it only reads
    what the App published. The `still` step requires the end, its attribution and the retired
    subscription to stay true for the whole window — a late second end or a late value would
    break one of them.
    """
    return tuple(
        _resume()
        + [
            {"op": "tap", "id": "disconnect-peripheral"},
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {
                    "link": "disconnected",
                    "linkEnd": "requested",
                    "notify": "ended",
                },
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": "severed"},
            {
                "op": "still",
                "id": STATUS_ID,
                "status": {
                    "link": "disconnected",
                    "linkEnd": "requested",
                    "notify": "ended",
                },
                "for": PASSIVE_SILENCE_SECONDS,
                "timeout": 30,
            },
            {"op": "read", "id": STATUS_ID, "as": "silent"},
        ]
        + [
            {"op": "wait", "id": identifier, "enabled": False, "timeout": 15}
            for identifier in CONTROLS_REQUIRING_LINK
        ]
        + _close()
    )


def _recovery_scan(
    cycle: int,
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
) -> tuple[dict[str, Any], ...]:
    """One cycle's preamble on the **same owner**: scan, link, discover, read.

    Only the first cycle opens the session; every later one re-enters the App it left, which
    is exactly what makes a recovery a claim about the owner surviving rather than about a
    scan happening. The read is attributed by the settled count, because the App keeps the
    previous cycle's rendering on screen.
    """
    head = (
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        if cycle == 0
        else _resume()
    )
    return tuple(
        head
        + _scan_stop(fresh_install if cycle == 0 else False, expected_local_name)
        # The target input belongs to the App, not to the cycle: only the first one types.
        + _declare_target(characteristic_uuid, type_it=cycle == 0)
        + [
            {"op": "tap", "id": "gatt-read"},
            # A scan is a run in the App: `start-scan` gives it a fresh read lane and view, so
            # every cycle's read is the *first* settled read of its own run. Measured on the
            # device: the count returns to zero when the next cycle starts scanning.
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"gattRead": "done", "gattReadTotal": "1"},
                "timeout": 60,
            },
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 30,
            },
            {"op": "read", "id": "gatt-result", "as": f"cycle-{cycle}-value"},
            {"op": "read", "id": STATUS_ID, "as": f"cycle-{cycle}-served"},
        ]
    )


def _recovery_announce(cycle: int, *, last: bool) -> tuple[dict[str, Any], ...]:
    """The peer's end: the App announces it, and nothing that needs the link is offered."""
    return tuple(
        _resume()
        + [
            {
                "op": "wait",
                "id": STATUS_ID,
                "status": {"link": "disconnected", "linkEnd": "unrequested"},
                "timeout": 90,
            },
            {"op": "read", "id": STATUS_ID, "as": f"cycle-{cycle}-announced"},
            {"op": "wait", "id": "gatt-read", "enabled": False, "timeout": 15},
        ]
        + (_close() if last else [])
    )


def _io(
    fresh_install: bool,
    attach: bool,
    expected_local_name: str | None,
    service_filter: str | None,
    characteristic_uuid: str,
    write_text: str,
) -> tuple[dict[str, Any], ...]:
    """Read, write, read back and read RSSI on one live link.

    The write payload is the host's, derived from the profile: this plan only carries it. A
    write receipt is not a stored value, so the characteristic is read again; the host
    compares the two readings and refuses an unchanged one.
    """
    return tuple(
        _prefix(
            fresh_install,
            attach,
            service_filter,
            DEFAULT_OBSERVATION_TIMEOUT_MS,
        )
        + _scan_stop(fresh_install, expected_local_name)
        + _declare_target(characteristic_uuid)
        + [
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 60,
            },
            # Read names are the receipt's keys and a plan may prefix the same scene with
            # its own reads (`initial` is the session precondition), so every name here is
            # namespaced by what produced it. A reused name would overwrite the earlier
            # value and make the receipt describe a different read than the plan asked for.
            {"op": "read", "id": "gatt-result", "as": "io-initial"},
            {
                "op": "setText",
                "id": "gatt-write-value",
                "text": write_text,
                "timeout": 30,
            },
            {"op": "tap", "id": "gatt-write"},
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": WRITE_RENDERING,
                "timeout": 60,
            },
            {"op": "read", "id": "gatt-result", "as": "io-written"},
            {"op": "tap", "id": "gatt-read"},
            {
                "op": "wait",
                "id": "gatt-result",
                "labelContains": VALUE_RENDERING,
                "timeout": 60,
            },
            {"op": "read", "id": "gatt-result", "as": "io-readback"},
            {"op": "tap", "id": "read-rssi"},
            {
                "op": "wait",
                "id": "rssi-value",
                "labelContains": "dBm",
                "timeout": 30,
            },
            {"op": "read", "id": "rssi-value", "as": "io-rssi"},
        ]
        + _close()
    )


# One `handoff` step: the driver announces a host action and waits for its confirmation.
# The timeout is the driver's own bound on how long the host may take.
HANDOFF_TIMEOUT_SECONDS = 180


def handoff_step(
    action: str, *, timeout: float = HANDOFF_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Suspend the driver while the host performs [action] in the same App process."""
    if type(action) is not str or not action.strip():
        raise ValueError("a handoff step needs its host action")
    return {"op": "handoff", "action": action, "timeout": timeout}


def plans(
    *,
    fresh_install: bool = False,
    attach: bool = False,
    expected_local_name: str | None = None,
    service_filter: str | None = None,
    subscription: dict[str, str] | None = None,
    gatt: dict[str, str] | None = None,
    scene: str = "smoke",
    cycles: int | None = None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Every plan this lane can run, composed for one situation.

    A managed scene may be several plans: `scene_ios_native` owns that shape, this module
    only knows steps. `subscription` carries the profile-derived target
    (`uuid`, `mode`, `value`) a subscription scene needs, `gatt` the profile-derived target
    (`uuid`, plus `write` for a scene that writes), and — for a repeating scene — its `scene`
    name and how many `cycles` it repeats, so neither module hardcodes any of them.
    """
    plans_: dict[str, tuple[dict[str, Any], ...]] = {
        "smoke": _smoke(fresh_install, attach),
        "scan": _scan(fresh_install, attach, expected_local_name, service_filter),
        "probe": PROBE_PLAN,
        # No target: the scene never reaches a characteristic, so its plans exist for every
        # situation rather than only when a profile target was derived.
        "connect-failure.open": _connect_failure_open(
            fresh_install, attach, expected_local_name, service_filter
        ),
        "connect-failure.attempt": _connect_failure_attempt(),
        # No target either: the platform answers every fact about the link itself.
        "link-facts": _link_facts(
            fresh_install, attach, expected_local_name, service_filter
        ),
        # No target: the system inventory is a fact about the adapter, not the GATT topology.
        "devices-query": _devices_query(
            fresh_install, attach, expected_local_name, service_filter
        ),
        # No target either: the platform owns the wait, and the scene never reaches a
        # characteristic. Three phases because the host withdraws and restores the peer.
        "auto-connect.open": _auto_connect_open(
            fresh_install, attach, expected_local_name, service_filter
        ),
        "auto-connect.arm": _auto_connect_arm(),
        "auto-connect.settle": _auto_connect_settle(),
        # No target and no peer: the two inventories the main line never needs.
        "devices-kinds": _devices_kinds(fresh_install, attach, service_filter),
        "adapter-control": _adapter_control(fresh_install, attach, service_filter),
    }
    if gatt is not None:
        plans_["cancellation.open"] = _cancellation_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            gatt["uuid"],
            CANCELLATION_TIMEOUT_MS,
        )
        plans_["cancellation.hold"] = _cancellation_hold()
        plans_["cancellation.queue"] = _cancellation_queue(gatt["uuid"])
        plans_["cancellation.settle"] = _cancellation_settle()
    if subscription is not None:
        hint = " ".join(
            subscription["value"][index : index + 2]
            for index in range(0, min(8, len(subscription["value"])), 2)
        )
        plans_["ccc-enable-reject.open"] = _ccc_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            subscription["uuid"],
        )
        plans_["ccc-enable-reject.enable"] = _ccc_enable_hold()
        plans_["ccc-enable-reject.settle"] = _ccc_enable_settle(subscription["mode"])
        plans_["ccc-enable-reject.collect"] = _collect(hint)
        plans_["ccc-disable-reject.open"] = _ccc_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            subscription["uuid"],
        )
        # The value can only be delivered to an established subscription, so establishing
        # one is its own phase: the host's emission happens between it and the pull.
        plans_["ccc-disable-reject.subscribe"] = tuple(
            _resume()
            + [
                {"op": "tap", "id": "gatt-subscribe"},
                {
                    "op": "wait",
                    "id": STATUS_ID,
                    "status": {
                        "notify": "subscribed",
                        "notifyMode": subscription["mode"],
                    },
                    "timeout": 60,
                },
                {"op": "read", "id": STATUS_ID, "as": "subscribed"},
            ]
        )
        plans_["ccc-disable-reject.value"] = tuple(_resume() + _pull_value(hint))
        plans_["ccc-disable-reject.hold"] = _ccc_disable_hold()
        plans_["ccc-disable-reject.probe"] = _ccc_disable_probe()
        text = subscription["value"]
        plans_["background.open"] = _ccc_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            subscription["uuid"],
        )
        plans_["background.subscribe"] = tuple(
            _resume()
            + [
                {"op": "tap", "id": "gatt-subscribe"},
                {
                    "op": "wait",
                    "id": STATUS_ID,
                    "status": {
                        "notify": "subscribed",
                        "notifyMode": subscription["mode"],
                    },
                    "timeout": 60,
                },
                {"op": "read", "id": STATUS_ID, "as": "subscribed"},
            ]
        )
        plans_["background.value"] = tuple(_resume() + _pull_value(hint))
        # Leaving the App is a step of its own, and it states the window it holds: the next
        # phase's host action (the emission) must happen while it is away, and this phase
        # fails unless it really is — for the declared dwell, not merely at one instant.
        plans_["background.away"] = (
            {"op": "background", "for": BACKGROUND_DWELL_SECONDS},
        )
        plans_["background.back"] = tuple(
            _resume()
            # The read that proves the case is the one the host emitted while the App was
            # away; it is still queued, so this pull settles immediately.
            + [{"op": "read", "id": STATUS_ID, "as": "resumed"}]
            + _pull_value(text)
            # The pull's own wait is satisfied by the value that was already on screen before
            # the App left, so it proves nothing about the queued one: on 2026-11-26 this
            # scene failed with
            # `notifyTotal=1` at `drained` and `notifyTotal=2` a moment later at `closed`.
            # The counter is the witness, so the plan waits for it by name instead of reading
            # a frame the previous cycle could already explain.
            + [
                {
                    "op": "wait",
                    "id": STATUS_ID,
                    "status": {"notifyTotal": "2"},
                    "timeout": 90,
                }
            ]
            + [{"op": "read", "id": STATUS_ID, "as": "drained"}]
            + _close()
        )
        for name in ("notification", "indication"):
            plans_[f"{name}.subscribe"] = _subscribe(
                fresh_install,
                attach,
                expected_local_name,
                service_filter,
                subscription["uuid"],
                subscription["mode"],
            )
            plans_[f"{name}.collect"] = _collect(hint)
    if cycles is not None:
        if scene is None:
            raise ValueError("a repeating scene needs its own name")
        if gatt is None:
            raise ValueError("a repeating scene needs the profile-derived target")
        for cycle in range(cycles):
            plans_[f"{scene}.scan:{cycle}"] = _recovery_scan(
                cycle,
                fresh_install,
                attach,
                expected_local_name,
                service_filter,
                gatt["uuid"],
            )
            plans_[f"{scene}.announce:{cycle}"] = _recovery_announce(
                cycle, last=cycle + 1 == cycles
            )
    if gatt is not None:
        plans_["passive-disconnect.open"] = _passive_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            gatt["uuid"],
        )
        plans_["passive-disconnect.ended"] = _passive_ended()
    if gatt is not None and subscription is not None:
        # The operator's end needs both targets: the read and the subscription are the same
        # profile-declared characteristic, and this scene is what proves they die together.
        plans_["disconnect.open"] = _disconnect_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            gatt["uuid"],
            subscription["mode"],
        )
        plans_["disconnect.value"] = tuple(_resume() + _pull_value(hint))
        plans_["disconnect.sever"] = _disconnect_sever()
        # Both targets, like the operator's end: the descriptor belongs to the characteristic
        # the subscription is established on.
        plans_["descriptor-io"] = _descriptor_io(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            gatt["uuid"],
            subscription["mode"],
        )
    if gatt is not None and "write" in gatt:
        plans_["write-reject.open"] = _write_reject_open(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            gatt["uuid"],
            gatt["write"],
        )
        plans_["write-reject.dispatch"] = _write_reject_dispatch()
        plans_["write-reject.settle"] = _write_reject_settle()
        plans_["io"] = _io(
            fresh_install,
            attach,
            expected_local_name,
            service_filter,
            gatt["uuid"],
            gatt["write"],
        )
    return plans_


PLANS = plans()
SMOKE_PLAN = PLANS["smoke"]
SCAN_PLAN = PLANS["scan"]
