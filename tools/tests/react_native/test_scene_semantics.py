"""What a managed scene *means*, driven without any platform.

`scene_native` owns the scene vocabulary and its derivations: which phases run, where the host
must act between two runs of the App, how one process-wide receipt is sliced back into phase
views, what each scene's own receipt must prove, and what a failed scene still publishes. That
logic used to be reachable only through the iOS executor's tests, which made an Android-only
bug look like an iOS-only bug — and priced every new scene as an xctestrun plus a patched lane.

Two drivers carry the tests here, and both implement only the seams the scene driver declares
(artifact, platform refusal, candidate binding, and one plan run or one phase run):

* `_InProcessDriver` runs a whole scene as one process, so a test about the vocabulary —
  phases, host actions, slicing, publishing — needs no receipt at all.
* `_ScriptedDriver` answers one phase at a time with a result the test wrote, so a test about a
  *derivation* supplies only "what the App said" and never touches a platform module.

A test that needs a real lane belongs in that lane's own module; a test that needs only the
vocabulary or a derivation belongs here. Every managed scene's derivation is covered here, and
the lane modules pin only what their executor alone can answer (artifact, plan naming, attach
mode, exit codes, exported attachments).
"""

from __future__ import annotations

import json
import time
import types
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tests.peripherals.nrf52840.fault_control import REJECT_ATT_RESULT
from tools.react_native import run_status
from tools.react_native.bleng_scan_evidence import (
    ADAPTER_CONTROL_SCENE,
    AUTO_CONNECT_SCENE,
    BACKGROUND_SCENE,
    CANCELLATION_SCENE,
    CCC_DISABLE_REJECT_SCENE,
    CCC_ENABLE_REJECT_SCENE,
    CONNECT_FAILURE_SCENE,
    DESCRIPTOR_IO_SCENE,
    DEVICES_KINDS_SCENE,
    DEVICES_QUERY_SCENE,
    DISCONNECT_SCENE,
    INDICATION_SCENE,
    IO_SCENE,
    LINK_FACTS_SCENE,
    NOTIFICATION_SCENE,
    PASSIVE_DISCONNECT_SCENE,
    RECOVERY_SCENE,
    SCAN_SCENE,
    SCENES,
    WRITE_REJECT_SCENE,
    render_gatt_value,
)
from tools.react_native.scene_native import (
    _PASSIVE_LEDGER,
    _RECOVERY_CYCLE_LEDGER,
    AUTO_CONNECT_LEDGER,
    CANCELLATION_LEDGER_PREFIX,
    CONNECT_FAILURE_LEDGER,
    DESCRIPTOR_IO_LEDGER,
    DEVICES_KINDS_LEDGER,
    DEVICES_QUERY_LEDGER,
    DISCONNECT_LEDGER,
    GATT_TARGET_KEYS,
    IO_LEDGER,
    LINK_FACTS_LEDGER,
    SCAN_LEDGER,
    SUBSCRIPTION_KINDS,
    SUBSCRIPTION_LEDGER,
    UNSUPPORTED_CODE,
    WRITE_REJECT_LEDGER,
    NativeSceneDriver,
    Phase,
)

LOCAL_NAME = "BleHubMultiRfV2DKA"
DEVICE = "00000000-0000-0000-0000-000000000000"
# The one frame shape every read is decoded as; this test is about slicing, not about
# which facts a scene asserts.
FRAME = ";".join(f"{key}=none" for key in run_status.KEYS)
SERVICE_UUID = "fd600000-0000-4000-8000-000000000000"
SUBSCRIPTION = {
    "uuid": "fd600000-0000-4000-8000-000000000001",
    "mode": "notification",
    "value": "ef",
}
ADVERTISED = f'exists label=name="{LOCAL_NAME}"; rssi=-50'
# The one read/write target these tests declare, and the capabilities the profile reports for
# it. A scene may only attempt what the peer's own topology says it supports.
IO = {"uuid": "fd600000-0000-4000-8000-000000000001", "write": "7"}
IO_PROPERTIES = "read,write,writeNoResponse"


def _frame(**facts: str) -> dict[str, str]:
    """One `run-status` read: every key the App renders, with these facts overriding `none`."""
    return {**{key: "none" for key in run_status.KEYS}, **facts}


def _ledger(*actions: str) -> str:
    """The App's own ordered action record, as it renders it."""
    entries = "\n".join(
        f"{index}|{action}|succeeded|" for index, action in enumerate(actions, start=1)
    )
    return f"omitted=0\n{entries}"


def _scan_projection() -> dict[str, dict[str, str]]:
    return {
        "initial": _frame(state="idle", page="Tests", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(
            state="started", scan="running", scanTotal="2", scanFilter=SERVICE_UUID
        ),
        "stopped": _frame(state="started", scan="stopped"),
        "closed": _frame(state="closed", scan="ownerClosed"),
    }


def _subscription_projection(
    mode: str = "notification",
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """The two phases of a subscription scene: what it established, what it collected."""
    subscribe = {
        "initial": _frame(state="idle", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(state="started", scan="running", scanTotal="2"),
        "stopped": _frame(state="started", scan="stopped"),
        "linked": _frame(state="started", link="connected"),
        "topology": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattTarget=SUBSCRIPTION["uuid"],
        ),
        "subscribed": _frame(
            state="started",
            link="connected",
            gatt="ready",
            notify="subscribed",
            notifyMode=mode,
            gattMatch="one",
        ),
    }
    collect = {
        "notified": _frame(
            state="started",
            notify="subscribed",
            notifyTotal="1",
            notifyMode=mode,
        ),
        "ended": _frame(state="started", notify="ended", notifyTotal="1"),
        "closed": _frame(state="closed", notify="ended", notifyTotal="1"),
    }
    return subscribe, collect


def _io_projection(*, properties: str = IO_PROPERTIES) -> dict[str, dict[str, str]]:
    """The read/write scene's one phase: what it reached, and what the peer declared."""
    return {
        "initial": _frame(state="idle", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(state="started", scan="running", scanTotal="2"),
        "stopped": _frame(state="started", scan="stopped"),
        "linked": _frame(state="started", link="connected"),
        "topology": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattTarget=IO["uuid"],
            gattProps=properties,
        ),
        "closed": _frame(state="closed", link="connected", gatt="ready"),
    }


def _io_values(
    *, readback: str = "1 bytes [37]", rssi: str = "-53 dBm"
) -> dict[str, str]:
    """What the App rendered for this scene's four outcomes."""
    characteristic = IO["uuid"]
    return {
        "io-initial": f"{characteristic}: 1 bytes [00]",
        "io-written": f"{characteristic}: wrote 1 bytes (withResponse)",
        "io-readback": f"{characteristic}: {readback}",
        "io-rssi": rssi,
    }


def _disconnect_projection() -> dict[str, dict[str, dict[str, str]]]:
    """An operator end: the lane the link carried, then the end the App submitted."""
    return {
        "open": {
            "initial": _frame(state="idle", scan="idle"),
            "started": _frame(state="started", scan="idle"),
            "scanning": _frame(state="started", scan="running", scanTotal="2"),
            "stopped": _frame(state="started", scan="stopped"),
            "linked": _frame(state="started", link="connected"),
            "topology": _frame(
                state="started",
                link="connected",
                gatt="ready",
                gattMatch="one",
                gattTarget=IO["uuid"],
                gattProps=IO_PROPERTIES,
            ),
            "subscribed": _frame(
                state="started",
                link="connected",
                gatt="ready",
                gattMatch="one",
                notify="subscribed",
                notifyMode=SUBSCRIPTION["mode"],
            ),
        },
        "value": {
            "notified": _frame(
                state="started",
                notify="subscribed",
                notifyMode=SUBSCRIPTION["mode"],
                notifyTotal="1",
            ),
        },
        "sever": {
            "severed": _frame(
                state="started",
                link="disconnected",
                linkEnd="requested",
                notify="ended",
                notifyTotal="1",
            ),
            "silent": _frame(
                state="started",
                link="disconnected",
                linkEnd="requested",
                notify="ended",
                notifyTotal="1",
            ),
            "closed": _frame(
                state="closed",
                link="disconnected",
                linkEnd="requested",
                notify="ended",
            ),
        },
    }


def _disconnect_ledger(*, disconnect_wait: str = "succeeded") -> str:
    """The App's own record: reach, read, subscribe, one value, then the end it submitted."""
    detail = (
        "operation/bleng.timeout: expired" if disconnect_wait != "succeeded" else ""
    )
    return _ledger_from(
        [
            (
                action,
                disconnect_wait if action == "disconnect-wait" else "succeeded",
                detail if action == "disconnect-wait" else "",
            )
            for action in DISCONNECT_LEDGER
        ]
    )


def _connect_failure_projection() -> dict[str, dict[str, dict[str, str]]]:
    """The peer was found, then the attempt it could not answer."""
    return {
        "open": {
            "initial": _frame(state="idle", scan="idle"),
            "started": _frame(state="started", scan="idle"),
            "scanning": _frame(state="started", scan="running", scanTotal="2"),
            "stopped": _frame(state="started", scan="stopped"),
        },
        "attempt": {
            "refused": _frame(
                state="started",
                link="none",
                linkEnd="none",
                failure="observation/bleng.timeout",
            ),
            "silent": _frame(
                state="started",
                link="none",
                linkEnd="none",
                failure="observation/bleng.timeout",
            ),
            "closed": _frame(state="closed", link="none", linkEnd="none"),
        },
    }


def _connect_failure_ledger(
    *,
    wait_outcome: str = "failed",
    detail: str = "observation/bleng.timeout: the wait expired",
    extra: tuple[str, ...] = (),
) -> str:
    """The App's own record of one accepted submission and one failed wait."""
    entries = [
        (
            action,
            wait_outcome if action == "connect-wait" else "succeeded",
            detail if action == "connect-wait" else "",
        )
        for action in CONNECT_FAILURE_LEDGER[:-1]
    ]
    entries += [(action, "succeeded", "") for action in extra]
    entries.append(("close", "succeeded", ""))
    return _ledger_from(entries)


def _link_facts_projection(*, unsupported: bool = False) -> dict[str, dict[str, str]]:
    """One live link and the four answers the platform gave about it."""
    refusal = f"submission/{UNSUPPORTED_CODE}"
    # Android's link starts at the default ATT MTU (23 -> 20-byte payload) and moves after a
    # request; a platform that refuses the request must report the limits it already had.
    limits = "20/20"
    after = "244/244" if not unsupported else limits

    def answered(key: str) -> dict[str, str]:
        if not unsupported:
            return _frame(state="started", link="connected", **{key: "done"})
        return _frame(
            state="started",
            link="connected",
            failure=refusal,
            **{key: "unsupported"},
        )

    return {
        "facts": {
            "initial": _frame(state="idle", scan="idle"),
            "started": _frame(state="started", scan="idle"),
            "scanning": _frame(state="started", scan="running", scanTotal="2"),
            "stopped": _frame(state="started", scan="stopped"),
            "linked": _frame(state="started", link="connected"),
            "mtu": _frame(
                state="started",
                link="connected",
                mtu="done",
                mtuWrites=limits,
            ),
            "mtuRequest": answered("mtuRequest"),
            "mtuAfter": _frame(
                state="started",
                link="connected",
                mtu="done",
                mtuWrites=after,
            ),
            "priority": answered("priority"),
            "refresh": answered("refresh"),
            "closed": _frame(state="closed", link="connected"),
        }
    }


def _link_facts_ledger(*, unsupported: bool = False, detail: str | None = None) -> str:
    """The App's own record: one submission and one wait per fact."""
    refusal = (
        detail
        if detail is not None
        else f"submission/{UNSUPPORTED_CODE}: no capability"
    )
    refused = (
        {"mtu-request-wait", "priority-wait", "refresh-wait"} if unsupported else set()
    )
    return _ledger_from(
        [
            (action, "failed", refusal)
            if action in refused
            else (action, "succeeded", "")
            for action in LINK_FACTS_LEDGER
        ]
    )


def _descriptor_io_projection(
    mode: str = "notification",
) -> dict[str, dict[str, dict[str, str]]]:
    """One live link whose CCCD tracks the App's subscription in both directions."""
    enabled = "0100" if mode == "notification" else "0200"
    return {
        "descriptor": {
            "initial": _frame(state="idle", scan="idle"),
            "started": _frame(state="started", scan="idle"),
            "scanning": _frame(state="started", scan="running", scanTotal="2"),
            "stopped": _frame(state="started", scan="stopped"),
            "linked": _frame(state="started", link="connected"),
            "topology": _frame(
                state="started",
                link="connected",
                gatt="ready",
                gattMatch="one",
                gattTarget=SUBSCRIPTION["uuid"],
                gattProps="read,notify,indicate",
            ),
            "cccd-initial": _frame(
                state="started",
                link="connected",
                descriptor="done",
                descriptorValue="0000",
            ),
            "subscribed": _frame(
                state="started", link="connected", notify="subscribed", notifyMode=mode
            ),
            "cccd-enabled": _frame(
                state="started",
                link="connected",
                descriptor="done",
                descriptorValue=enabled,
            ),
            "ended": _frame(state="started", link="connected", notify="ended"),
            "cccd-cleared": _frame(
                state="started",
                link="connected",
                descriptor="done",
                descriptorValue="0000",
            ),
            "cccd-written": _frame(
                state="started",
                link="connected",
                descriptor="done",
                descriptorValue="0000",
                descriptorWrite="done",
            ),
            "cccd-unchanged": _frame(
                state="started",
                link="connected",
                descriptor="done",
                descriptorValue="0000",
            ),
            "closed": _frame(state="closed", link="connected"),
        }
    }


def _devices_query_projection(
    *, devices: str = "done", count: str = "1", peer: str = "yes"
) -> dict[str, dict[str, dict[str, str]]]:
    """One live link whose adapter reports it in the `connected` inventory."""
    return {
        "devices": {
            "initial": _frame(state="idle", scan="idle"),
            "started": _frame(state="started", scan="idle"),
            "scanning": _frame(state="started", scan="running", scanTotal="2"),
            "stopped": _frame(state="started", scan="stopped"),
            "linked": _frame(state="started", link="connected"),
            "devices": _frame(
                state="started",
                link="connected",
                devices=devices,
                devicesCount=count,
                devicesPeer=peer,
            ),
            "closed": _frame(state="closed", link="connected"),
        }
    }


def _auto_connect_projection(
    *, answer: str = "done", link: str = "connected", failure: str = "none"
) -> dict[str, dict[str, dict[str, str]]]:
    """Three phases: the peer is found, the standing request is made while it is away, then
    the platform's answer is read after the host restores it."""
    return {
        "open": {
            "initial": _frame(state="idle", scan="idle"),
            "started": _frame(state="started", scan="idle"),
            "scanning": _frame(state="started", scan="running", scanTotal="2"),
            "stopped": _frame(state="started", scan="stopped"),
        },
        "arm": {
            "submitted": _frame(
                state="started", scan="stopped", auto="pending", link="connecting"
            ),
        },
        "settle": {
            "auto": _frame(state="started", auto=answer, link=link, failure=failure),
            "closed": _frame(state="closed"),
        },
    }


def _devices_kinds_projection(
    *, bonded: str = "done", companion: str = "done"
) -> dict[str, dict[str, dict[str, str]]]:
    """Two inventory probes and no peer: each answer belongs to the platform."""

    def answered(kind: str, answer: str) -> dict[str, str]:
        if answer == "done":
            return _frame(
                state="started", devices="done", devicesKind=kind, devicesCount="0"
            )
        return _frame(
            state="started",
            devices="unsupported",
            devicesKind=kind,
            failure="submission/" + UNSUPPORTED_CODE,
        )

    return {
        "kinds": {
            "initial": _frame(state="idle"),
            "started": _frame(state="started"),
            "bonded": answered("bonded", bonded),
            "companion": answered("companion", companion),
            "closed": _frame(state="closed"),
        }
    }


def _adapter_control_projection(
    *, off: str = "unsupported", on: str = "unsupported"
) -> dict[str, dict[str, dict[str, str]]]:
    """Two radio requests and no peer: each answer belongs to the platform."""

    def answered(direction: str, answer: str) -> dict[str, str]:
        # `scan=idle` is the App's own phase on a session that never scanned: the scene proves a
        # radio request leaves the observation main line alone.
        if answer == "done":
            return _frame(
                state="started", scan="idle", radio="done", radioTarget=direction
            )
        return _frame(
            state="started",
            scan="idle",
            radio="unsupported",
            radioTarget=direction,
            failure="submission/" + UNSUPPORTED_CODE,
        )

    return {
        "radio": {
            "initial": _frame(state="idle"),
            "started": _frame(state="started"),
            "off": answered("off", off),
            "on": answered("on", on),
            "closed": _frame(state="closed"),
        }
    }


class _Emitter:
    """The peer's own emission, with the role it was asked to emit on."""

    def __init__(self, value: bytes) -> None:
        self.value = value
        self.roles: list[str] = []

    def publish_notification(self, role: str) -> bytes:
        self.roles.append(role)
        return self.value


class _Peer:
    """The peer: emit a value, arm/confirm/resolve one fault, drop the link, offer itself again.

    `PeerHold` re-runs the caller's ownership guard around every one of these, so a scene
    that lost fixture ownership stops before the next wire command. A test that only wants to
    know *which* commands a scene issued records them here instead of touching a device —
    and a scene that emitted a value it never asked for fails instead of passing.
    """

    def __init__(self, value: bytes | None = None) -> None:
        self.value = value
        self.calls: list[str] = []
        self.roles: list[str] = []

    def publish_notification(self, role: str) -> bytes:
        if self.value is None:
            raise RuntimeError(f"this scene must not emit, but it emitted on {role!r}")
        self.roles.append(role)
        self.calls.append(f"publish:{role}")
        return self.value

    def drop_peer(self) -> bytes:
        self.calls.append("drop")
        return b"\x11\x22"

    def resume_advertising(self, *, expect_adoption: bool = False) -> None:
        # The fake records the declaration too, so a scene that asks for the strict answer and
        # one that states adoption are distinguishable without a device.
        self.calls.append(
            "resume" if not expect_adoption else "resume:adoption-expected"
        )

    def withdraw_advertising(self) -> None:
        self.calls.append("withdraw")

    def arm_hold(self, operation, role, requested, *, before=None):
        self.calls.append(f"arm:{operation.name}:{role}:{requested.hex()}")
        if before is not None:
            self.calls.append(f"before:{before.hex()}")
        return "armed"

    def require_hold_held(self, armed) -> None:
        self.calls.append(f"held:{armed}")

    def release_hold(self, armed, decision) -> str:
        self.calls.append(f"release:{decision.name}")
        return "receipt"


def _write_reject_projection() -> dict[str, dict[str, dict[str, str]]]:
    """The three phases of the refused-write scene: reach, parked write, settlement."""
    opened = {
        "initial": _frame(state="idle", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(state="started", scan="running", scanTotal="2"),
        "stopped": _frame(state="started", scan="stopped"),
        "linked": _frame(state="started", link="connected"),
        "topology": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattTarget=IO["uuid"],
            gattProps=IO_PROPERTIES,
        ),
    }
    dispatch = {
        "dispatched": _frame(state="started", link="connected", gatt="ready"),
    }
    # The refusal is the peer's own ATT status, so the App's failure identity is that status
    # and nothing else. The value is still the pre-write one, which is what makes the
    # refusal meaningful: a refused write changed nothing.
    settle = {
        "rejected": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattRead="done",
            gattReadTotal="1",
            failure=f"apple/{REJECT_ATT_RESULT}",
        ),
        "closed": _frame(state="closed", failure=f"apple/{REJECT_ATT_RESULT}"),
    }
    return {"open": opened, "dispatch": dispatch, "settle": settle}


def _dispatch_ledger(*, pending: bool = True) -> str:
    """The App's record at the moment the parked write is still outstanding."""
    waited = "pending" if pending else "succeeded"
    return "\n".join(
        [
            "omitted=0",
            "1|start|succeeded|",
            "2|gatt-write|succeeded|",
            f"3|gatt-write-wait|{waited}|",
        ]
    )


def _write_reject_ledger(*, outcome: str = "failed", detail: str | None = None) -> str:
    """The App's own record: one refused write wait, in the sequence this scene ran.

    The sequence comes from the contract (`WRITE_REJECT_LEDGER`), so a change to what this
    scene records cannot leave the fake describing an older scene.
    """
    refused = detail or (
        f"gatt/gatt: the peer refused the write [apple/{REJECT_ATT_RESULT} "
        "CBATTErrorDomain: The value's length is invalid.]"
    )
    lines = ["omitted=0"]
    marked = False
    for sequence, action in enumerate(WRITE_REJECT_LEDGER, start=1):
        if action == "gatt-write-wait" and not marked:
            marked = True
            lines.append(f"{sequence}|{action}|{outcome}|{refused}")
            continue
        lines.append(f"{sequence}|{action}|succeeded|")
    return "\n".join(lines)


def _cancellation_projection() -> dict[str, dict[str, dict[str, str]]]:
    """The four phases of the cancellation scene: reach, held read, queued read, recovery."""
    opened = {
        "initial": _frame(state="idle", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(state="started", scan="running", scanTotal="2"),
        "stopped": _frame(state="started", scan="stopped"),
        "linked": _frame(state="started", link="connected"),
        "topology": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattTarget=IO["uuid"],
            gattProps=IO_PROPERTIES,
        ),
    }
    hold = {
        "held": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattRead="waiting",
            gattReads="1",
        ),
    }
    queue = {
        "queued": _frame(state="started", gattRead="waiting", gattReads="2"),
        "cancelled": _frame(state="started", gattRead="cancelled", gattReads="1"),
    }
    settle = {
        "completed": _frame(
            state="started", gattRead="done", gattReads="0", gattReadTotal="2"
        ),
        "recovered": _frame(
            state="started", gattRead="done", gattReads="0", gattReadTotal="3"
        ),
        "closed": _frame(state="closed", gattRead="done", gattReadTotal="3"),
    }
    return {"open": opened, "hold": hold, "queue": queue, "settle": settle}


def _cancellation_ledger(
    *,
    cancel: bool = True,
    detail: str = "operation/cancelled: BLeng operation was cancelled",
) -> str:
    """The App's own record: three read submissions, exactly one cancelled wait, one close.

    The prefix is the shared read/write preamble, taken from the contract rather than copied,
    so a change to what every read scene records cannot leave this fake behind.
    """
    entries: list[tuple[str, str, str]] = [
        (action, "succeeded", "") for action in CANCELLATION_LEDGER_PREFIX
    ]
    for index in range(3):
        entries.append(("gatt-read", "succeeded", ""))
        if index == 1 and cancel:
            entries.append(("gatt-read-wait", "failed", detail))
            entries.append(("gatt-read-cancel", "succeeded", ""))
        else:
            entries.append(("gatt-read-wait", "succeeded", ""))
    entries.append(("close", "succeeded", ""))
    lines = ["omitted=0"]
    for sequence, (action, outcome, text) in enumerate(entries, start=1):
        lines.append(f"{sequence}|{action}|{outcome}|{text}")
    return "\n".join(lines)


def _ccc_open_projection(
    *, properties: str = "read,notify,indicate"
) -> dict[str, dict[str, str]]:
    """The preamble every CCC scene shares: reach the peer, resolve the descriptor."""
    return {
        "initial": _frame(state="idle", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(state="started", scan="running", scanTotal="2"),
        "stopped": _frame(state="started", scan="stopped"),
        "linked": _frame(state="started", link="connected"),
        "topology": _frame(
            state="started",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattTarget=SUBSCRIPTION["uuid"],
            gattProps=properties,
        ),
    }


def _ccc_enable_projection(
    mode: str = "notification",
) -> dict[str, dict[str, dict[str, str]]]:
    """A refused enable, the retry that establishes, and the value it then delivers."""
    return {
        "open": _ccc_open_projection(),
        "enable": {"held": _frame(state="started", notify="enabling")},
        "settle": {
            "rejected": _frame(
                state="started",
                notify="failed",
                failure=f"apple/{REJECT_ATT_RESULT}",
            ),
            "recovered": _frame(
                state="started",
                notify="subscribed",
                notifyMode=mode,
            ),
        },
        "collect": {
            "notified": _frame(
                state="started",
                notify="subscribed",
                notifyMode=mode,
                notifyTotal="1",
            ),
            "ended": _frame(state="started", notify="ended", notifyTotal="1"),
            "closed": _frame(state="closed", notify="ended"),
        },
    }


def _ccc_enable_ledger(*, failure: str | None = None) -> str:
    """One refused enable wait, one accepted retry, one value, one disable."""
    entries: list[tuple[str, str, str]] = [
        (action, "succeeded", "")
        for action in (
            "start",
            "begin-scan",
            "scan-start-wait",
            "scan-pull",
            "scan-stop",
            "connect",
            "connect-wait",
            "gatt-discover",
            "gatt-discover-wait",
            "notify-enable",
        )
    ]
    entries.append(
        (
            "notify-enable-wait",
            "failed",
            failure or f"[apple/{REJECT_ATT_RESULT} CBATTErrorDomain: Unlikely error.]",
        )
    )
    entries.extend(
        [
            ("notify-enable", "succeeded", ""),
            ("notify-enable-wait", "succeeded", ""),
            ("notify-pull", "succeeded", ""),
            ("notify-disable", "succeeded", ""),
            ("notify-disable-wait", "succeeded", ""),
            ("notify-pull", "succeeded", ""),
            ("close", "succeeded", ""),
        ]
    )
    return _ledger_from(entries)


def _ccc_disable_projection(
    *, delivered: bool, mode: str = "notification"
) -> dict[str, dict[str, dict[str, str]]]:
    """A refused disable, the probe that decides what the platform still does, its end."""
    return {
        "open": _ccc_open_projection(),
        "subscribe": {
            "subscribed": _frame(state="started", notify="subscribed", notifyMode=mode),
        },
        "value": {
            "notified": _frame(
                state="started",
                notify="subscribed",
                notifyMode=mode,
                notifyTotal="1",
            ),
        },
        "hold": {"held": _frame(state="started", notify="subscribed")},
        "probe": {
            "rejected": _frame(
                state="started",
                notify="unverified",
                failure=f"apple/{REJECT_ATT_RESULT}",
            ),
            "probed": _frame(
                state="started",
                notify="subscribed" if delivered else "unverified",
                notifyMode=mode,
                notifyTotal="1" if delivered else "2",
            ),
            "ended": _frame(state="started", notify="ended"),
            "closed": _frame(state="closed", notify="ended"),
        },
    }


def _ccc_disable_ledger(
    *, delivered: bool, probe_failure: str = "observation/bleng.timeout: expired"
) -> str:
    """One refused disable wait, the probe pull, then the disable that settles.

    A probe that found the lane silent is the only case where the pull itself may fail, and
    the App has to name that as an observation failure rather than an end.
    """
    entries: list[tuple[str, str, str]] = [
        (action, "succeeded", "")
        for action in (
            "start",
            "begin-scan",
            "scan-start-wait",
            "scan-pull",
            "scan-stop",
            "connect",
            "connect-wait",
            "gatt-discover",
            "gatt-discover-wait",
            "notify-enable",
            "notify-enable-wait",
            "notify-pull",
            "notify-disable",
        )
    ]
    entries.append(
        (
            "notify-disable-wait",
            "failed",
            f"[apple/{REJECT_ATT_RESULT} CBATTErrorDomain: Unlikely error.]",
        )
    )
    entries.append(
        (
            "notify-pull",
            "succeeded" if delivered else "failed",
            "" if delivered else probe_failure,
        )
    )
    entries.extend(
        [
            ("notify-disable", "succeeded", ""),
            ("notify-disable-wait", "succeeded", ""),
            ("notify-pull", "succeeded", ""),
            ("close", "succeeded", ""),
        ]
    )
    return _ledger_from(entries)


def _ledger_from(entries: list[tuple[str, str, str]]) -> str:
    lines = ["omitted=0"]
    for sequence, (action, outcome, detail) in enumerate(entries, start=1):
        lines.append(f"{sequence}|{action}|{outcome}|{detail}")
    return "\n".join(lines)


def _parked_ledger(action: str, outcome: str) -> str:
    """The App's record at the moment one submission's wait is outstanding."""
    return _ledger_from(
        [
            ("start", "succeeded", ""),
            (action, "succeeded", ""),
            (f"{action}-wait", outcome, ""),
        ]
    )


def _passive_projection() -> dict[str, dict[str, dict[str, str]]]:
    """A peer-initiated end: the reach, then the announcement the App made by itself."""
    settled = _frame(
        state="started",
        link="disconnected",
        linkEnd="unrequested",
        failure="gatt/peer",
    )
    return {
        "open": _ccc_open_projection(properties=IO_PROPERTIES),
        "ended": {
            "announced": _frame(
                state="started",
                link="disconnected",
                linkEnd="unrequested",
                failure="gatt/peer",
            ),
            "after": f"{IO['uuid']}: 1 bytes [00]",
            # The quiet sample is the same account, unchanged, for the declared window.
            "silent": dict(settled),
            "closed": _frame(state="closed", link="disconnected"),
        },
    }


def _passive_ledger(*, read_wait: str = "succeeded") -> str:
    """The App's own record: the preamble, the read that proved the link, the close.

    The sequence comes from the contract, so a change to what this scene records cannot leave
    the fake describing an older scene.
    """
    detail = "operation/bleng.timeout: expired" if read_wait != "succeeded" else ""
    return _ledger_from(
        [
            (action, read_wait if action == "gatt-read-wait" else "succeeded", detail)
            for action in _PASSIVE_LEDGER
        ]
    )


def _recovery_projection(cycles: int) -> dict[str, dict[str, dict[str, str]]]:
    """N repetitions of one cycle: reach the peer, read once, let the peer end it.

    Every phase states its own facts. A cycle's read is its *own* run's first settled read,
    which is why each one carries its own settled count rather than inheriting the previous
    cycle's — that inheritance is exactly what a stale session would look like.
    """
    phases: dict[str, dict[str, dict[str, str]]] = {}
    reach = _ccc_open_projection(properties=IO_PROPERTIES)
    for index in range(cycles):
        phases[f"scan:{index}"] = {
            **reach,
            "scanning": _frame(state="started", scan="running"),
            "stopped": _frame(state="started", scan="stopped"),
            "linked": _frame(state="started", scan="stopped", link="connected"),
            "topology": _frame(
                state="started",
                scan="stopped",
                link="connected",
                gatt="ready",
                gattMatch="one",
                gattTarget=IO["uuid"],
                gattProps=IO_PROPERTIES,
                gattRead="done",
                gattReadTotal="1",
            ),
            f"cycle-{index}-served": _frame(
                state="started",
                scan="stopped",
                link="connected",
                gatt="ready",
                gattMatch="one",
                gattRead="done",
                gattReadTotal="1",
            ),
        }
        phases[f"announce:{index}"] = {
            f"cycle-{index}-announced": _frame(
                state="started",
                link="disconnected",
                linkEnd="unrequested",
                failure="gatt/peer",
            ),
            "closed": _frame(state="closed", link="disconnected"),
        }
    return phases


def _recovery_ledger(cycles: int, *, read_wait: str = "succeeded") -> str:
    """One session's record: each cycle scans, links, discovers and reads on its own.

    A scan is a run in the App, so each cycle records its own preamble; only the first starts
    the session and only the last closes it.
    """
    entries: list[tuple[str, str, str]] = [("start", "succeeded", "")]
    detail = "operation/bleng.timeout: expired" if read_wait != "succeeded" else ""
    # One cycle records everything but the close; the session's close belongs to the last one,
    # which is why the contract's cycle template ends with it and this fake appends it once.
    cycle = tuple(action for action in _RECOVERY_CYCLE_LEDGER if action != "close")
    for _ in range(cycles):
        entries.extend(
            (action, read_wait if action == "gatt-read-wait" else "succeeded", detail)
            for action in cycle
        )
    entries.append(("close", "succeeded", ""))
    return _ledger_from(entries)


class _InProcessDriver(NativeSceneDriver):
    """Four seams, no platform: the smallest thing one scene can be driven by.

    The whole scene runs as one process, with the host's actions answered in band, which is
    what makes a test about the vocabulary (phases, slicing, publishing) need no receipt.
    """

    kind = "test-semantics"
    record_scope = "test-semantics-scene"
    one_process = True

    def __init__(self, *, fail: bool = False, **kwargs: Any) -> None:
        self.fail = fail
        self.seams: list[str] = []
        super().__init__(**kwargs)

    def _artifact(self) -> dict[str, Any]:
        return {"test_artifact_sha256": "0" * 64}

    def _require_artifact(self) -> None:
        self.seams.append("artifact")

    def _require_platform(self, *, platform: str, candidate: Any, phone: Any) -> None:
        if platform != "test":
            raise TypeError("this driver is test-only")

    def _bind_candidate(self, candidate: Any, *, require_owner: Any, deadline: float):
        self.seams.append("bind")
        return lambda: None

    def _run_scene_plan(
        self, raw: Path, options: dict[str, Any], *, timeout: float, on_handoff: Any
    ) -> dict[str, Any]:
        self.seams.append("run")
        if self.fail:
            raise RuntimeError("the App never answered")
        steps = self._merged_steps()
        # Every host action the plan announced is answered in band, once per occurrence.
        for step in steps:
            if step.get("op") == "handoff":
                on_handoff(step["action"])
        rows = [
            {"index": index, "step": step, "seconds": 0.0, "observed": {}}
            for index, step in enumerate(steps)
        ]
        return {
            "status": "passed",
            "steps": rows,
            "values": {"ledger": "omitted=0\n1|start|succeeded|\n"},
        }


class _ScriptedDriver(_InProcessDriver):
    """The scene driven by what the App answered, one result per phase.

    `_run_phase` is the executor seam, so a test about a *derivation* supplies the receipt and
    needs no lane, no pinned artifact and no patch of platform code. Overriding the seam
    (rather than patching a module) is also what keeps each test replacing exactly one thing —
    the App's answers — which is convention C1.
    """

    one_process = False

    def __init__(self, *, results: dict[str, dict[str, Any]], **kwargs: Any) -> None:
        self.results = results
        super().__init__(**kwargs)

    def _run_phase(
        self, phase: Phase, raw: Path, options: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        self.seams.append(f"phase:{phase.name}")
        result = self.results[phase.name]
        if result.get("status") != "passed":
            raise RuntimeError(str(result.get("primary_error") or "phase failed"))
        return result


class SceneSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.output = self.root / "attempt" / "driver"
        self.run_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _driver(self, *, scene=NOTIFICATION_SCENE, fail: bool = False, **extra):
        if scene.kind in SUBSCRIPTION_KINDS:
            extra.setdefault("subscription", SUBSCRIPTION)
        return _InProcessDriver(
            scene=scene,
            device=DEVICE,
            app_id="test.bundle",
            output=self.output,
            cwd=self.root,
            expected_local_name=LOCAL_NAME,
            fail=fail,
            **extra,
        )

    def _ready(self, driver: _InProcessDriver) -> _InProcessDriver:
        driver._run_id = self.run_id
        return driver

    def _driver_for(self, scene):
        """Any managed scene, with exactly the targets its own contract requires."""
        extra: dict[str, Any] = {}
        if scene.kind in SUBSCRIPTION_KINDS:
            extra["subscription"] = SUBSCRIPTION
        required = GATT_TARGET_KEYS.get(scene.kind)
        if required is not None:
            extra["gatt"] = {key: IO[key] for key in required}
        return self._driver(scene=scene, **extra)

    def _options(self) -> dict[str, Any]:
        return {
            "timeout": 30.0,
            "observations": [],
            "require_app_authority": lambda: None,
            "fixture": None,
        }

    # -- vocabulary -------------------------------------------------------------

    def test_a_host_action_is_one_row_between_the_phases(self) -> None:
        # `notification` is `subscribe`, then the host emits, then `collect`. The handoff is
        # a step of the merged plan because a subscription can only receive a value after the
        # peer's CCC is written — it is not a second run of the App.
        driver = self._driver()
        steps = driver._merged_steps()
        handoffs = [
            index for index, step in enumerate(steps) if step.get("op") == "handoff"
        ]
        self.assertEqual(len(handoffs), 1, steps)
        handoff = steps[handoffs[0]]
        self.assertEqual(handoff["action"], "publish")
        subscribe = driver._steps(Phase("subscribe"))
        self.assertEqual(
            handoffs[0], len(subscribe), "the host acts after the subscribing phase"
        )
        # A read name that two phases could both use is prefixed with its phase, so one
        # process-wide receipt cannot overwrite the earlier value.
        reads = [step for step in steps if step.get("op") == "read"]
        self.assertTrue(reads)
        for step in reads:
            self.assertIn("::", step["as"], step)

    def test_the_slices_are_cut_by_the_plan_that_was_built(self) -> None:
        driver = self._ready(self._driver())
        steps = driver._merged_steps()
        # A passing receipt is exactly the plan; each read contributes its own value under
        # the name the plan emitted, which is how the slice restores plan-local names.
        values = {}
        for phase in driver._phases:
            for step in driver._steps(phase):
                if step.get("op") == "read":
                    values[f"{phase.name}::{step['as']}"] = FRAME
        document = {
            "status": "passed",
            "receipt": {
                "status": "passed",
                "steps": [
                    {"index": index, "step": step, "seconds": 0.0, "observed": {}}
                    for index, step in enumerate(steps)
                ],
                "values": values,
            },
        }

        sliced = driver._split_phases(document)

        self.assertEqual(sorted(sliced), sorted(phase.name for phase in driver._phases))
        for phase in driver._phases:
            view = sliced[phase.name]
            self.assertEqual(
                [row["step"] for row in view["receipt"]["steps"]],
                list(driver._steps(phase)),
                f"{phase.name} kept another phase's rows",
            )
            expected = {
                step["as"]: FRAME
                for step in driver._steps(phase)
                if step.get("op") == "read"
            }
            self.assertEqual(view["receipt"]["values"], expected, phase.name)

    def test_a_short_receipt_is_refused_only_when_it_claims_to_have_passed(
        self,
    ) -> None:
        driver = self._ready(self._driver())
        steps = driver._merged_steps()
        short = {
            "status": "passed",
            "receipt": {
                "status": "passed",
                "steps": [
                    {"index": index, "step": step, "seconds": 0.0, "observed": {}}
                    for index, step in enumerate(steps[:-1])
                ],
                "values": {},
            },
        }
        with self.assertRaisesRegex(RuntimeError, "shorter than the plan"):
            driver._split_phases(short)
        # A failed receipt is a prefix of its plan by definition, and slicing it as far as
        # it goes is what lets a failed scene still explain itself.
        failed = {
            **short,
            "receipt": {**short["receipt"], "status": "failed"},
        }
        sliced = driver._split_phases(failed)
        self.assertTrue(sliced)

    # -- publishing -------------------------------------------------------------

    def test_a_failed_scene_publishes_its_record_and_still_raises(self) -> None:
        driver = self._ready(self._driver(scene=SCAN_SCENE, fail=True))
        with self.assertRaisesRegex(RuntimeError, "never answered"):
            driver.drive(SCAN_SCENE, self._options())
        published = {path.name for path in self.output.iterdir()}
        self.assertEqual(published, {"plan.json", "record.json"})
        record = json.loads((self.output / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertIn("never answered", record["primary_error"])
        self.assertEqual(record["kind"], "test-semantics")
        self.assertEqual(record["scope"], "test-semantics-scene")

    def test_the_driver_is_one_shot(self) -> None:
        # `require_handoff` is the one entry that binds a scene to a run, and it may succeed
        # exactly once: a second call would let one scene inherit another's identity.
        driver = self._driver(scene=SCAN_SCENE)
        phone = types.SimpleNamespace(device="00000000-0000-0000-0000-000000000000")
        arguments = {
            "platform": "test",
            "candidate": None,
            "phone": phone,
            "run_id": self.run_id,
            "app_id": "test.bundle",
            "deadline": time.monotonic() + 60.0,
        }
        handoff = driver.require_handoff(**arguments)
        self.assertEqual(handoff["kind"], "test-semantics")
        self.assertEqual(handoff["session_id"], f"test-semantics:{self.run_id}")
        # `require_handoff` is the only caller of the artifact seam: the handoff states the
        # exact bytes this run will drive, before anything is installed.
        self.assertEqual(handoff["test_artifact_sha256"], "0" * 64)
        self.assertEqual(driver.seams, [], "no device work before the plan is made")
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            driver.require_handoff(**arguments)

    def test_an_unknown_scene_is_refused_before_the_driver_exists(self) -> None:
        from tools.react_native.bleng_scan_evidence import SceneEvidence

        with self.assertRaisesRegex(ValueError, "no native plan yet"):
            self._driver(
                scene=SceneEvidence(name="no-such-scene", rf="x", trace=("x",))
            )

    # -- the scan scene ----------------------------------------------------------

    def test_a_scan_that_matched_the_fixture_is_the_whole_trace(self) -> None:
        driver = self._scripted(
            SCAN_SCENE,
            {"scan": _scan_projection()},
            ledgers={"scan": _ledger(*SCAN_LEDGER)},
        )
        options = self._options()
        driver.drive(SCAN_SCENE, options)
        self.assertEqual(options["observations"], list(SCAN_SCENE.trace))
        # Nothing here is a platform fact: one plan, one receipt, one derived trace.
        self.assertEqual(driver.seams, ["phase:scan"])

    def test_an_advertisement_that_is_not_the_fixture_cannot_pass(self) -> None:
        # The pull loop reports what it saw; a scene that accepted any nearby advertisement
        # would be evidence about the room, not about the peer this run leased.
        driver = self._scripted(
            SCAN_SCENE,
            {"scan": _scan_projection()},
            ledgers={"scan": _ledger(*SCAN_LEDGER)},
            advertised='exists label=name="neighbour"; rssi=-50',
        )
        with self.assertRaisesRegex(RuntimeError, "does not name the fixture"):
            driver.drive(SCAN_SCENE, self._options())

    def test_a_ledger_that_does_not_describe_the_scene_cannot_pass(self) -> None:
        # The App's own ordered record is the second witness: it says the operations ran in
        # the order the scene claims, independently of the projection it renders.
        driver = self._scripted(
            SCAN_SCENE,
            {"scan": _scan_projection()},
            ledgers={
                "scan": _ledger("start", "begin-scan", "scan-start-wait", "close")
            },
        )
        with self.assertRaisesRegex(RuntimeError, "do not describe this scene"):
            driver.drive(SCAN_SCENE, self._options())

    def test_a_contradictory_projection_cannot_pass(self) -> None:
        # The trace is derived from what the App published, so a receipt whose facts
        # contradict the scene must fail here instead of producing an accepted trace.
        projection = _scan_projection()
        projection["scanning"]["scan"] = "failed"
        driver = self._scripted(
            SCAN_SCENE,
            {"scan": projection},
            ledgers={"scan": _ledger(*SCAN_LEDGER)},
        )
        with self.assertRaisesRegex(RuntimeError, "scanning.scan"):
            driver.drive(SCAN_SCENE, self._options())

    # -- the subscription scene --------------------------------------------------

    def test_a_subscription_keeps_the_emission_between_its_two_phases(self) -> None:
        # The scene's claim is an ordering the App cannot produce alone: the subscription is
        # established before the host emits, so the value's arrival is evidence about the
        # subscription rather than about the App's own state.
        subscribe, collect = _subscription_projection()
        driver = self._scripted(
            NOTIFICATION_SCENE,
            {"subscribe": subscribe, "collect": collect},
            ledgers={
                "subscribe": _ledger("start"),
                "collect": _ledger(*SUBSCRIPTION_LEDGER),
            },
        )
        emitter = _Emitter(bytes.fromhex(SUBSCRIPTION["value"]))
        # One list for both witnesses: a phase boundary and an authority check interleave,
        # and their exact order is what says the host acted between two App runs.
        order = driver.seams
        options = self._options()
        options.update(
            fixture=emitter, require_app_authority=lambda: order.append("authority")
        )

        driver.drive(NOTIFICATION_SCENE, options)

        self.assertEqual(emitter.roles, [NOTIFICATION_SCENE.characteristic_role])
        self.assertEqual(
            order, ["phase:subscribe", "authority", "authority", "phase:collect"]
        )
        self.assertEqual(options["observations"], list(NOTIFICATION_SCENE.trace))

    def test_an_emission_the_profile_does_not_declare_is_refused(self) -> None:
        # The peer emits on a role, and the bytes it emits are the profile's, not the
        # fixture's idea of them: a different value would make the collected observation
        # describe a scene the profile never declared.
        subscribe, _ = _subscription_projection()
        driver = self._scripted(
            NOTIFICATION_SCENE,
            {"subscribe": subscribe, "collect": {}},
            ledgers={"subscribe": _ledger("start")},
        )
        options = self._options()
        options.update(fixture=_Emitter(b"\xff\xff"))
        with self.assertRaisesRegex(RuntimeError, "does not declare"):
            driver.drive(NOTIFICATION_SCENE, options)

    def test_an_indication_subscription_is_the_same_scene_with_its_own_mode(
        self,
    ) -> None:
        # `indication` is the same derivation as `notification` with the other mode, and the
        # mode is asserted rather than assumed: the App has to report what it actually
        # established, so one platform cannot silently deliver the other kind.
        subscribe, collect = _subscription_projection(mode="indication")
        driver = self._scripted(
            INDICATION_SCENE,
            {"subscribe": subscribe, "collect": collect},
            ledgers={
                "subscribe": _ledger("start"),
                "collect": _ledger(*SUBSCRIPTION_LEDGER),
            },
            subscription={**SUBSCRIPTION, "mode": "indication"},
        )
        options = self._options()
        options.update(fixture=_Emitter(bytes.fromhex(SUBSCRIPTION["value"])))

        driver.drive(INDICATION_SCENE, options)

        self.assertEqual(options["observations"], list(INDICATION_SCENE.trace))

    def test_an_indication_the_app_reports_as_a_notification_cannot_pass(self) -> None:
        subscribe, collect = _subscription_projection()
        driver = self._scripted(
            INDICATION_SCENE,
            {"subscribe": subscribe, "collect": collect},
            ledgers={
                "subscribe": _ledger("start"),
                "collect": _ledger(*SUBSCRIPTION_LEDGER),
            },
            subscription={**SUBSCRIPTION, "mode": "indication"},
        )
        options = self._options()
        options.update(fixture=_Emitter(bytes.fromhex(SUBSCRIPTION["value"])))
        with self.assertRaisesRegex(RuntimeError, "notifyMode is 'notification'"):
            driver.drive(INDICATION_SCENE, options)

    # -- the read/write scene ----------------------------------------------------

    def _io(self, *, projection: dict | None = None, values: dict | None = None):
        """A read/write scene answered correctly, then bent by one fact."""
        return self._scripted(
            IO_SCENE,
            {"io": projection or _io_projection()},
            gatt=IO,
            ledgers={"io": _ledger(*IO_LEDGER)},
            values={"io": values or _io_values()},
        )

    def test_an_io_scene_proves_the_peer_stored_the_write(self) -> None:
        driver = self._io()
        options = self._options()
        driver.drive(IO_SCENE, options)
        self.assertEqual(options["observations"], list(IO_SCENE.trace))
        # What a reviewer reads back: one phase, its own plan, and no fixture action — the
        # read/write scene never needs the host to touch the peer between two App runs.
        record = json.loads(
            (self.last_output / "record.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["plan"], "io")
        self.assertEqual([phase["name"] for phase in record["phases"]], ["io"])
        self.assertIsNone(record["phases"][0]["host"])

    def test_a_read_back_that_did_not_change_cannot_pass(self) -> None:
        # A write receipt proves the App handed the bytes over; only the second read proves
        # the peer stored them, so an unchanged value is a failure rather than a no-op.
        driver = self._io(values=_io_values(readback="1 bytes [00]"))
        with self.assertRaisesRegex(RuntimeError, "not a stored value"):
            driver.drive(IO_SCENE, self._options())

    def test_a_reading_routed_through_another_characteristic_is_refused(self) -> None:
        values = _io_values()
        values["io-readback"] = f"{SERVICE_UUID}: 1 bytes [37]"
        driver = self._io(values=values)
        with self.assertRaisesRegex(RuntimeError, "another characteristic"):
            driver.drive(IO_SCENE, self._options())

    def test_a_write_receipt_is_not_a_read_value(self) -> None:
        values = _io_values()
        values["io-initial"] = f"{IO['uuid']}: wrote 1 bytes (withResponse)"
        driver = self._io(values=values)
        with self.assertRaisesRegex(RuntimeError, "not a value rendering"):
            driver.drive(IO_SCENE, self._options())

    def test_a_target_the_peer_cannot_serve_is_refused_before_any_io(self) -> None:
        for properties, message in (
            ("write,writeNoResponse", "not readable"),
            ("read,notify", "not writable"),
        ):
            with (
                self.subTest(properties=properties),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self._io(projection=_io_projection(properties=properties)).drive(
                    IO_SCENE, self._options()
                )

    def test_a_connected_rssi_must_be_a_negative_reading(self) -> None:
        for reading, message in (
            ("0 dBm", "must be negative"),
            ("RSSI not read", "not a reading"),
        ):
            with (
                self.subTest(reading=reading),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self._io(values=_io_values(rssi=reading)).drive(
                    IO_SCENE, self._options()
                )

    def test_the_io_ledger_must_show_the_write_between_its_two_reads(self) -> None:
        # The projection says a value changed; the App's own ordered record says the change
        # happened because of the write, and not between two reads it happened to make.
        driver = self._scripted(
            IO_SCENE,
            {"io": _io_projection()},
            gatt=IO,
            ledgers={"io": _ledger("start", "gatt-read", "gatt-read-wait", "close")},
            values={"io": _io_values()},
        )
        with self.assertRaisesRegex(RuntimeError, "do not describe this scene"):
            driver.drive(IO_SCENE, self._options())

    # -- construction -------------------------------------------------------------

    def test_a_scene_must_name_the_peer_it_drives(self) -> None:
        # Without a name the pull loop would succeed on any nearby advertisement, so this is
        # a refusal rather than a default.
        with self.assertRaisesRegex(ValueError, "expected local name"):
            _InProcessDriver(
                scene=SCAN_SCENE,
                device=DEVICE,
                app_id="test.bundle",
                output=self.output,
                cwd=self.root,
            )

    def test_a_scene_that_reads_a_profile_target_needs_one(self) -> None:
        # The lane never chooses a characteristic: the producer derives it from the profile,
        # and a scene that would read or subscribe without one has nothing to address.
        for scene in (
            NOTIFICATION_SCENE,
            IO_SCENE,
            CANCELLATION_SCENE,
            WRITE_REJECT_SCENE,
            CCC_ENABLE_REJECT_SCENE,
            CCC_DISABLE_REJECT_SCENE,
            PASSIVE_DISCONNECT_SCENE,
            DISCONNECT_SCENE,
        ):
            with (
                self.subTest(scene=scene.name),
                self.assertRaisesRegex(ValueError, "profile-derived"),
            ):
                self._driver(scene=scene, subscription=None, gatt=None)

    def test_a_scene_cannot_declare_a_target_it_does_not_drive(self) -> None:
        # A key no scene uses is refused instead of ignored: an ignored target is a scene
        # that silently did not exercise what its caller asked for.
        with self.assertRaisesRegex(ValueError, "declares no read/write target"):
            self._driver(scene=SCAN_SCENE, gatt=IO)

    def test_an_io_payload_the_write_input_cannot_carry_is_refused(self) -> None:
        # The App encodes its write input as UTF-8 text, so the payload has to be printable
        # ASCII of the declared length; anything else would write different bytes.
        for payload in ("", " 7", "7\n", "\x00", "é", "7" * 17):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(ValueError, "printable ASCII"),
            ):
                self._driver(
                    scene=IO_SCENE, gatt={"uuid": IO["uuid"], "write": payload}
                )

    def test_the_run_budget_must_be_usable(self) -> None:
        driver = self._driver(scene=SCAN_SCENE)
        with self.assertRaisesRegex(ValueError, "finite positive budget"):
            driver.drive(SCAN_SCENE, {"timeout": 0, "observations": []})

    def test_every_managed_scene_kind_reaches_a_derivation(self) -> None:
        """A new scene with a phase list but no derivation would fail only on a phone.

        This drives the dispatcher with an empty phase map: the derivation's own complaint
        (a missing phase) is fine — what must never happen is "no observation derivation
        for a <kind> scene", because that scene could never pass anywhere.
        """
        for scene in SCENES.values():
            with self.subTest(scene=scene.name):
                driver = self._driver_for(scene)
                with self.assertRaises(Exception) as caught:
                    driver._observations({})
                self.assertNotIn(
                    "no observation derivation", str(caught.exception), scene.name
                )

    # -- the cancellation scene ---------------------------------------------------

    def _cancellation(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A cancelled queued read answered correctly, then bent by one fact."""
        driver = self._scripted(
            CANCELLATION_SCENE,
            projections or _cancellation_projection(),
            gatt={"uuid": IO["uuid"]},
            ledgers={
                "open": _ledger("start"),
                "hold": _ledger("start"),
                "queue": _ledger("start"),
                "settle": ledger if ledger is not None else _cancellation_ledger(),
            },
            values={
                "queue": {
                    "cancel": "operation/cancelled: BLeng operation was cancelled"
                },
                "settle": {
                    "completed-value": f"{IO['uuid']}: 1 bytes [00]",
                    "recovered-value": f"{IO['uuid']}: 1 bytes [00]",
                },
            },
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_a_cancellation_scene_holds_one_read_and_recovers(self) -> None:
        driver, fixture, options = self._cancellation()
        driver.drive(CANCELLATION_SCENE, options)
        self.assertEqual(options["observations"], list(CANCELLATION_SCENE.trace))
        # The host armed before the second run, confirmed the firmware held the request
        # before the third and released it before the fourth: those three commands are the
        # only reason the phases exist, so their order is part of the scene.
        self.assertEqual(
            [call.split(":")[0] for call in fixture.calls],
            ["arm", "held", "release"],
        )
        record = json.loads(
            (self.last_output / "record.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [phase["host"] for phase in record["phases"]],
            [None, "hold-arm", "hold-verify", "hold-release"],
        )

    def test_a_read_that_was_not_queued_cannot_pass(self) -> None:
        # One outstanding read is the held one; the queued read only exists because the App
        # accepted a second submission while the peer was still holding the first.
        projections = _cancellation_projection()
        projections["queue"]["queued"] = _frame(
            state="started", gattRead="waiting", gattReads="1"
        )
        driver, _, options = self._cancellation(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "gattReads is '1'"):
            driver.drive(CANCELLATION_SCENE, options)

    def test_a_cancel_that_retired_the_held_read_cannot_pass(self) -> None:
        # A queue depth of zero would mean the cancel also retired the peer-held read, which
        # is a different scene: the read the App cancelled is the queued one.
        projections = _cancellation_projection()
        projections["queue"]["cancelled"] = _frame(
            state="started", gattRead="cancelled", gattReads="0"
        )
        driver, _, options = self._cancellation(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "gattReads is '0'"):
            driver.drive(CANCELLATION_SCENE, options)

    def test_a_recovery_read_must_be_its_own_settled_read(self) -> None:
        # The App keeps showing the previous read's rendering, so only the settled count
        # says the recovery read happened at all.
        projections = _cancellation_projection()
        projections["settle"]["recovered"] = _frame(
            state="started", gattRead="done", gattReads="0", gattReadTotal="2"
        )
        driver, _, options = self._cancellation(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "gattReadTotal is '2'"):
            driver.drive(CANCELLATION_SCENE, options)

    def test_the_ledger_must_show_exactly_one_cancelled_wait(self) -> None:
        # Every wait succeeded: nothing was actually cancelled, so the derived
        # `cancel:queued-accepted` would be a claim about a request, not about a result.
        driver, _, options = self._cancellation(
            ledger=_cancellation_ledger(cancel=False)
        )
        with self.assertRaisesRegex(RuntimeError, "not this scene"):
            driver.drive(CANCELLATION_SCENE, options)

    def test_a_cancelled_wait_must_name_the_cancellation(self) -> None:
        # A failed wait for any other reason is not a cancellation: the App has to name the
        # terminal it observed, and the contract's word for this one is `cancelled`.
        driver, _, options = self._cancellation(
            ledger=_cancellation_ledger(detail="peer reset the link")
        )
        with self.assertRaisesRegex(RuntimeError, "not this scene"):
            driver.drive(CANCELLATION_SCENE, options)

    # -- the refused-write scene --------------------------------------------------

    def _write_reject(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
        values: dict | None = None,
        dispatched: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A refused write that recovered correctly, then bent by one fact."""
        characteristic = IO["uuid"]
        receipt_values = {
            "initial-value": f"{characteristic}: 1 bytes [00]",
            "unchanged": f"{characteristic}: 1 bytes [00]",
            "rejection": (
                f"gatt/gatt: the peer refused the write [apple/{REJECT_ATT_RESULT} "
                "CBATTErrorDomain: The value's length is invalid.]"
            ),
            "recovered": f"{characteristic}: wrote 1 bytes (withResponse)",
            "stored": f"{characteristic}: 1 bytes [37]",
        }
        receipt_values.update(values or {})
        driver = self._scripted(
            WRITE_REJECT_SCENE,
            projections or _write_reject_projection(),
            gatt=IO,
            ledgers={
                "open": _ledger("start"),
                "dispatch": (
                    dispatched if dispatched is not None else _dispatch_ledger()
                ),
                "settle": ledger if ledger is not None else _write_reject_ledger(),
            },
            values={"open": receipt_values, "settle": receipt_values},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_a_refused_write_changes_nothing_and_then_succeeds(self) -> None:
        driver, fixture, options = self._write_reject()
        driver.drive(WRITE_REJECT_SCENE, options)
        self.assertEqual(options["observations"], list(WRITE_REJECT_SCENE.trace))
        # The peer is armed for exactly the bytes this scene asks for, confirmed while the
        # App's wait is outstanding, and then refuses: three commands, in that order.
        self.assertEqual(
            fixture.calls,
            [
                "arm:RESPONSE_WRITE:{role}:{payload}".format(
                    role=WRITE_REJECT_SCENE.characteristic_role,
                    payload=IO["write"].encode("ascii").hex(),
                ),
                "held:armed",
                "release:REJECT",
            ],
        )
        record = json.loads(
            (self.last_output / "record.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [phase["host"] for phase in record["phases"]],
            [None, "hold-arm", "hold-reject"],
        )

    def test_a_refusal_that_is_not_the_peer_s_status_cannot_pass(self) -> None:
        # The refusal has to be the ATT status the firmware contract declares; a local
        # timeout or an observation error is a different failure wearing the same scene.
        driver, _, options = self._write_reject(
            values={
                "rejection": (
                    "gatt/gatt: the read timed out [apple/6 CBErrorDomain: timed out.]"
                )
            }
        )
        with self.assertRaisesRegex(RuntimeError, "does not name the peer"):
            driver.drive(WRITE_REJECT_SCENE, options)

    def test_a_value_that_moved_under_a_refusal_cannot_pass(self) -> None:
        driver, _, options = self._write_reject(
            values={"unchanged": f"{IO['uuid']}: 1 bytes [37]"}
        )
        with self.assertRaisesRegex(RuntimeError, "changed its value"):
            driver.drive(WRITE_REJECT_SCENE, options)

    def test_the_read_back_after_the_recovered_write_must_be_the_request(self) -> None:
        driver, _, options = self._write_reject(
            values={"stored": f"{IO['uuid']}: 1 bytes [00]"}
        )
        with self.assertRaisesRegex(RuntimeError, "after a successful write"):
            driver.drive(WRITE_REJECT_SCENE, options)

    def test_a_payload_the_peer_already_holds_cannot_prove_anything(self) -> None:
        # If the peer already stores the declared payload, "the value changed" can only mean
        # the scene wrote something else — so this is a refusal, not a weaker pass.
        held = f"{IO['uuid']}: {render_gatt_value(IO['write'].encode('ascii'))}"
        driver, _, options = self._write_reject(
            values={"initial-value": held, "unchanged": held}
        )
        with self.assertRaisesRegex(RuntimeError, "already the value the peer holds"):
            driver.drive(WRITE_REJECT_SCENE, options)

    def test_the_write_must_have_been_accepted_and_still_waiting_when_parked(
        self,
    ) -> None:
        # Nothing is published while a write waits, so the App's own record is the only
        # proof that a request was accepted and had not settled when its peer was parked.
        driver, _, options = self._write_reject(
            dispatched=_dispatch_ledger(pending=False)
        )
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            driver.drive(WRITE_REJECT_SCENE, options)

    def test_the_failed_wait_must_be_the_only_failure(self) -> None:
        driver, _, options = self._write_reject(
            ledger=_write_reject_ledger(outcome="succeeded")
        )
        with self.assertRaisesRegex(RuntimeError, "only failure"):
            driver.drive(WRITE_REJECT_SCENE, options)

    def test_the_refused_wait_must_name_the_peer_s_status_in_the_record(self) -> None:
        driver, _, options = self._write_reject(
            ledger=_write_reject_ledger(
                detail="operation/bleng.timeout: the wait expired"
            )
        )
        with self.assertRaisesRegex(RuntimeError, "does not name the peer"):
            driver.drive(WRITE_REJECT_SCENE, options)

    # -- the refused-enable scene ------------------------------------------------

    def _ccc_enable(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
        values: dict | None = None,
        pending: str | None = None,
        mode: str = "notification",
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A refused CCC enable that recovers, then bent by one fact."""
        if pending is None:
            pending = _parked_ledger("notify-enable", "pending")
        receipt_values = {"rejected": f"apple/{REJECT_ATT_RESULT}"}
        receipt_values.update(values or {})
        driver = self._scripted(
            CCC_ENABLE_REJECT_SCENE,
            projections or _ccc_enable_projection(mode),
            ledgers={
                "open": _ledger("start"),
                "enable": pending,
                "settle": ledger if ledger is not None else _ccc_enable_ledger(),
                "collect": ledger if ledger is not None else _ccc_enable_ledger(),
            },
            values={"settle": receipt_values, "collect": receipt_values},
            subscription={**SUBSCRIPTION, "mode": mode},
        )
        fixture = _Peer(bytes.fromhex(SUBSCRIPTION["value"]))
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_a_refused_enable_is_a_failed_subscription_then_a_real_one(self) -> None:
        driver, fixture, options = self._ccc_enable()
        driver.drive(CCC_ENABLE_REJECT_SCENE, options)
        self.assertEqual(options["observations"], list(CCC_ENABLE_REJECT_SCENE.trace))
        # The peer is armed for this subscription's own CCC enable, refuses it, and only then
        # is asked to emit: the value that proves the retry worked is the host's, not the App's.
        self.assertEqual(
            [call.split(":")[0] for call in fixture.calls],
            ["arm", "held", "release", "publish"],
        )
        self.assertEqual(fixture.roles, [CCC_ENABLE_REJECT_SCENE.characteristic_role])

    def test_a_refused_enable_the_app_calls_subscribed_cannot_pass(self) -> None:
        projections = _ccc_enable_projection()
        projections["settle"]["rejected"] = _frame(
            state="started",
            notify="subscribed",
            notifyMode="notification",
        )
        driver, _, options = self._ccc_enable(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "notify is 'subscribed'"):
            driver.drive(CCC_ENABLE_REJECT_SCENE, options)

    def test_a_refused_enable_must_name_the_peer_s_status(self) -> None:
        projections = _ccc_enable_projection()
        projections["settle"]["rejected"] = _frame(
            state="started", notify="failed", failure="operation/bleng.timeout"
        )
        driver, _, options = self._ccc_enable(projections=projections)
        with self.assertRaisesRegex(
            RuntimeError, "did not report the peer's ATT status"
        ):
            driver.drive(CCC_ENABLE_REJECT_SCENE, options)

    def test_an_enable_that_was_never_outstanding_cannot_be_held(self) -> None:
        driver, _, options = self._ccc_enable(
            pending=_parked_ledger("notify-enable", "succeeded")
        )
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            driver.drive(CCC_ENABLE_REJECT_SCENE, options)

    def test_a_late_status_read_does_not_cost_the_scene_its_claim(self) -> None:
        # The status is transient: a device whose automation channel stalls for two minutes
        # (measured 2026-11-26, one press at 120.6 s) reads it after the state has settled.
        # The ledger is the proof that the submission was accepted while outstanding, so a
        # settled status must not fail a scene whose real fact is intact.
        projections = _ccc_enable_projection()
        projections["enable"]["held"] = _frame(state="started", notify="subscribed")
        driver, _, options = self._ccc_enable(projections=projections)
        driver.drive(CCC_ENABLE_REJECT_SCENE, options)
        self.assertEqual(options["observations"], list(CCC_ENABLE_REJECT_SCENE.trace))

    def test_an_enable_whose_handler_never_ran_cannot_be_held(self) -> None:
        # Tolerating a settled status is not tolerating silence: a scene whose handler never
        # published a notify state has nothing to hold, and the ledger alone would otherwise
        # be the only witness.
        projections = _ccc_enable_projection()
        projections["enable"]["held"] = _frame(state="started")
        driver, _, options = self._ccc_enable(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "published no notify state"):
            driver.drive(CCC_ENABLE_REJECT_SCENE, options)

    # -- the refused-disable scene -----------------------------------------------

    def _ccc_disable(
        self,
        *,
        delivered: bool = False,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
        branch: int | None = None,
        hold_ledger: str | None = None,
        mode: str = "notification",
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A refused CCC disable answered correctly, then bent by one fact."""
        if hold_ledger is None:
            hold_ledger = _parked_ledger("notify-disable", "pending")
        final = (
            ledger if ledger is not None else _ccc_disable_ledger(delivered=delivered)
        )
        driver = self._scripted(
            CCC_DISABLE_REJECT_SCENE,
            projections or _ccc_disable_projection(delivered=delivered, mode=mode),
            ledgers={
                "open": _ledger("start"),
                "subscribe": _ledger("start"),
                "value": _ledger("start"),
                "hold": hold_ledger,
                "probe": final,
            },
            values={"probe": {"rejected": f"apple/{REJECT_ATT_RESULT}"}},
            wait_any_for={
                "probe": branch if branch is not None else (0 if delivered else 1)
            },
            subscription={**SUBSCRIPTION, "mode": mode},
        )
        fixture = _Peer(bytes.fromhex(SUBSCRIPTION["value"]))
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_a_refused_disable_that_stops_delivery_is_probed_not_called_an_end(
        self,
    ) -> None:
        driver, _, options = self._ccc_disable(delivered=False)
        driver.drive(CCC_DISABLE_REJECT_SCENE, options)
        self.assertEqual(options["observations"], list(CCC_DISABLE_REJECT_SCENE.trace))

    def test_a_refused_disable_that_keeps_delivering_is_probed_too(self) -> None:
        driver, fixture, options = self._ccc_disable(delivered=True)
        driver.drive(CCC_DISABLE_REJECT_SCENE, options)
        self.assertEqual(options["observations"], list(CCC_DISABLE_REJECT_SCENE.trace))
        # The host emits while the subscription is established, then arms the disable (with the
        # enable it must re-check as pre-state) and refuses it: the value the App pulled is the
        # one the host emitted, not one the App invented.
        self.assertEqual(
            [call.split(":")[0] for call in fixture.calls],
            ["publish", "arm", "before", "held", "release"],
        )

    def test_the_probe_branch_must_match_what_the_app_reported(self) -> None:
        # A platform that stopped delivering may only say it does not know; a scene that
        # narrates that answer as "still subscribed" would claim a stream it cannot see.
        driver, _, options = self._ccc_disable(delivered=False, branch=0)
        with self.assertRaisesRegex(RuntimeError, "notify is 'unverified'"):
            driver.drive(CCC_DISABLE_REJECT_SCENE, options)

    def test_a_probe_failure_that_is_not_an_observation_failure_cannot_pass(
        self,
    ) -> None:
        driver, _, options = self._ccc_disable(
            delivered=False,
            ledger=_ccc_disable_ledger(
                delivered=False, probe_failure="gatt/gatt: peer refused"
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "not an observation failure"):
            driver.drive(CCC_DISABLE_REJECT_SCENE, options)

    def test_a_disable_that_was_never_outstanding_cannot_be_held(self) -> None:
        driver, _, options = self._ccc_disable(
            hold_ledger=_parked_ledger("notify-disable", "succeeded")
        )
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            driver.drive(CCC_DISABLE_REJECT_SCENE, options)

    def test_an_indication_ccc_enable_is_armed_for_the_indication_bits(self) -> None:
        # The firmware contract names the enable value per mode: `0100` for notification and
        # `0200` for indication. A scene that armed the wrong bits would hold nothing, so the
        # bytes the peer is armed for are part of the derivation's evidence.
        driver, fixture, options = self._ccc_enable(mode="indication")
        driver.drive(CCC_ENABLE_REJECT_SCENE, options)
        self.assertEqual(
            fixture.calls[0],
            f"arm:CCC_WRITE:{CCC_ENABLE_REJECT_SCENE.characteristic_role}:0200",
        )

    def test_an_indication_ccc_disable_rechecks_the_indication_enable(self) -> None:
        # A disable parks the zero value and states the enable it must re-check first.
        driver, fixture, options = self._ccc_disable(delivered=True, mode="indication")
        driver.drive(CCC_DISABLE_REJECT_SCENE, options)
        self.assertEqual(
            fixture.calls[1:3], ["arm:CCC_WRITE:updates:0000", "before:0200"]
        )

    # -- the peer-initiated end scene ---------------------------------------------

    def _passive(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
        values: dict | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A peer-initiated end answered correctly, then bent by one fact."""
        receipt_values = {
            "initial-value": f"{IO['uuid']}: 1 bytes [00]",
            "after": f"{IO['uuid']}: 1 bytes [00]",
        }
        receipt_values.update(values or {})
        driver = self._scripted(
            PASSIVE_DISCONNECT_SCENE,
            projections or _passive_projection(),
            gatt={"uuid": IO["uuid"]},
            ledgers={
                "open": _ledger("start"),
                "ended": ledger if ledger is not None else _passive_ledger(),
            },
            values={"open": receipt_values, "ended": receipt_values},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_a_peer_end_is_announced_silent_and_withdraws_controls(self) -> None:
        driver, fixture, options = self._passive()
        driver.drive(PASSIVE_DISCONNECT_SCENE, options)
        self.assertEqual(options["observations"], list(PASSIVE_DISCONNECT_SCENE.trace))
        # The host caused the end and the App was told nothing, so the only peer command is
        # the drop itself — nothing the scene narrates may come from a command it issued.
        self.assertEqual(fixture.calls, ["drop"])

    def test_an_end_attributed_to_nobody_when_the_app_requested_it_cannot_pass(
        self,
    ) -> None:
        projections = _passive_projection()
        projections["ended"]["announced"] = _frame(
            state="started", link="disconnected", linkEnd="requested"
        )
        driver, _, options = self._passive(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "linkEnd is 'requested'"):
            driver.drive(PASSIVE_DISCONNECT_SCENE, options)

    def test_a_value_that_appears_after_the_end_cannot_pass(self) -> None:
        driver, _, options = self._passive(
            values={"after": f"{IO['uuid']}: 1 bytes [37]"}
        )
        with self.assertRaisesRegex(RuntimeError, "a value appeared"):
            driver.drive(PASSIVE_DISCONNECT_SCENE, options)

    def test_an_account_that_changes_during_the_quiet_window_cannot_pass(self) -> None:
        projections = _passive_projection()
        projections["ended"]["silent"] = _frame(
            state="started",
            link="disconnected",
            linkEnd="unrequested",
            failure="observation/bleng.timeout",
        )
        driver, _, options = self._passive(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "changed its own account"):
            driver.drive(PASSIVE_DISCONNECT_SCENE, options)

    def test_the_quiet_window_must_have_been_sampled(self) -> None:
        # A receipt whose quiet step carries no sampled window is not a claim about time.
        driver, _, options = self._passive()
        for row in driver.results["ended"]["receipt"]["steps"]:
            row["observed"].pop("still", None)
        with self.assertRaisesRegex(RuntimeError, "quiet window"):
            driver.drive(PASSIVE_DISCONNECT_SCENE, options)

    def test_the_app_must_not_have_learned_the_end_from_a_failed_read(self) -> None:
        # The end lane announces it and nothing else does: a failed read in the record would
        # mean the App learned the end from a command instead of from the peer's own end.
        driver, _, options = self._passive(ledger=_passive_ledger(read_wait="failed"))
        with self.assertRaisesRegex(RuntimeError, "recorded a failed action"):
            driver.drive(PASSIVE_DISCONNECT_SCENE, options)

    # -- the operator's own end scene -----------------------------------------------

    def _disconnect(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
        values: dict | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """An operator end answered correctly, then bent by one fact."""
        receipt_values = {
            "disconnect-initial": f"{IO['uuid']}: 1 bytes [00]",
        }
        receipt_values.update(values or {})
        driver = self._scripted(
            DISCONNECT_SCENE,
            projections or _disconnect_projection(),
            gatt={"uuid": IO["uuid"]},
            ledgers={
                "open": _ledger("start"),
                "sever": ledger if ledger is not None else _disconnect_ledger(),
            },
            values={"open": receipt_values, "sever": receipt_values},
        )
        fixture = _Peer(bytes.fromhex(SUBSCRIPTION["value"]))
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_an_operator_end_holds_and_retires_what_the_link_carried(self) -> None:
        driver, fixture, options = self._disconnect()
        driver.drive(DISCONNECT_SCENE, options)
        self.assertEqual(options["observations"], list(DISCONNECT_SCENE.trace))
        # The host's only act is the emission that proves the subscription delivered: the end
        # itself is the operator's command, so the peer hears nothing about it.
        self.assertEqual(fixture.calls, ["publish:updates"])

    def test_a_subscription_still_claimed_after_the_end_cannot_pass(self) -> None:
        # The bug this scene exists to catch: a link the App itself reported dead is not a
        # link that can carry a live subscription.
        projections = _disconnect_projection()
        projections["sever"]["severed"] = _frame(
            state="started",
            link="disconnected",
            linkEnd="requested",
            notify="subscribed",
            notifyTotal="1",
        )
        driver, _, options = self._disconnect(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "notify is 'subscribed'"):
            driver.drive(DISCONNECT_SCENE, options)

    def test_an_end_the_app_blames_on_nobody_cannot_pass(self) -> None:
        # The mirror of `passive-disconnect`: an end this screen submitted must be attributed
        # to the operator, not reported as one nobody asked for.
        projections = _disconnect_projection()
        projections["sever"]["severed"] = _frame(
            state="started",
            link="disconnected",
            linkEnd="unrequested",
            notify="ended",
            notifyTotal="1",
        )
        driver, _, options = self._disconnect(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "linkEnd is 'unrequested'"):
            driver.drive(DISCONNECT_SCENE, options)

    def test_a_value_counted_across_the_end_cannot_pass(self) -> None:
        projections = _disconnect_projection()
        projections["sever"]["severed"] = _frame(
            state="started",
            link="disconnected",
            linkEnd="requested",
            notify="ended",
            notifyTotal="2",
        )
        driver, _, options = self._disconnect(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "counted a value across its own end"):
            driver.drive(DISCONNECT_SCENE, options)

    def test_a_record_without_the_disconnect_is_a_different_scene(self) -> None:
        # The ledger is what separates this scene from a peer's end: the same preamble and
        # read without the submitted command describe `passive-disconnect`, not this scene.
        driver, _, options = self._disconnect(ledger=_passive_ledger())
        with self.assertRaisesRegex(RuntimeError, "do not describe this scene"):
            driver.drive(DISCONNECT_SCENE, options)

    def test_a_disconnect_that_never_settled_cannot_pass(self) -> None:
        driver, _, options = self._disconnect(
            ledger=_disconnect_ledger(disconnect_wait="failed")
        )
        with self.assertRaisesRegex(RuntimeError, "recorded a failed action"):
            driver.drive(DISCONNECT_SCENE, options)

    def test_the_quiet_window_must_have_been_sampled_after_an_operator_end(
        self,
    ) -> None:
        driver, _, options = self._disconnect()
        for row in driver.results["sever"]["receipt"]["steps"]:
            row["observed"].pop("still", None)
        with self.assertRaisesRegex(RuntimeError, "quiet window"):
            driver.drive(DISCONNECT_SCENE, options)

    # -- the unanswerable connect scene ----------------------------------------------

    def _connect_failure(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A failed connect answered honestly, then bent by one fact."""
        driver = self._scripted(
            CONNECT_FAILURE_SCENE,
            projections or _connect_failure_projection(),
            ledgers={
                "open": _ledger("start"),
                "attempt": ledger if ledger is not None else _connect_failure_ledger(),
            },
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_an_unanswerable_connect_is_reported_and_never_becomes_a_link(self) -> None:
        driver, fixture, options = self._connect_failure()
        driver.drive(CONNECT_FAILURE_SCENE, options)
        self.assertEqual(options["observations"], list(CONNECT_FAILURE_SCENE.trace))
        # The host's only act is the withdrawal: the App is told nothing, and the peer hears
        # nothing about the attempt the central made.
        self.assertEqual(fixture.calls, ["withdraw"])

    def test_a_projection_that_disagrees_with_the_record_cannot_pass(self) -> None:
        # Two witnesses: the App's record says which failure happened, and its projection must
        # name the same identity. A `none` here would hide a failure behind a blank summary.
        projections = _connect_failure_projection()
        projections["attempt"]["refused"] = _frame(
            state="started", link="none", linkEnd="none", failure="none"
        )
        driver, _, options = self._connect_failure(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "failure is 'none'"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_a_failed_connect_that_claims_the_link_cannot_pass(self) -> None:
        projections = _connect_failure_projection()
        projections["attempt"]["refused"] = _frame(
            state="started",
            link="connected",
            linkEnd="none",
            failure="observation/bleng.timeout",
        )
        driver, _, options = self._connect_failure(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "link is 'connected'"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_a_failure_that_invents_an_end_cannot_pass(self) -> None:
        # Nothing began, so nothing ended: an end here would blame a peer or an operator for
        # a link that never existed.
        projections = _connect_failure_projection()
        projections["attempt"]["refused"] = _frame(
            state="started",
            link="none",
            linkEnd="unrequested",
            failure="observation/bleng.timeout",
        )
        driver, _, options = self._connect_failure(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "linkEnd is 'unrequested'"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_a_platform_status_in_the_record_is_the_identity_it_renders(self) -> None:
        # The other shape of a failure: a status the platform reported. The projection must
        # carry that status, not the App's own domain code.
        ledger = _connect_failure_ledger(
            detail="gatt/bleng.gatt.connect_failed: GATT error "
            "[android/133 BluetoothGatt]"
        )
        projections = _connect_failure_projection()
        for name in ("refused", "silent"):
            projections["attempt"][name] = _frame(
                state="started", link="none", linkEnd="none", failure="android/133"
            )
        driver, _, options = self._connect_failure(
            projections=projections, ledger=ledger
        )
        driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_a_connect_that_settled_cannot_pass_as_a_failure(self) -> None:
        # The scene exists to reject "still connecting" and "it worked".
        driver, _, options = self._connect_failure(
            ledger=_connect_failure_ledger(wait_outcome="succeeded")
        )
        with self.assertRaisesRegex(RuntimeError, "only failed action"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_an_action_after_the_failed_connect_cannot_pass(self) -> None:
        # A failed link cannot carry a read, a write or a subscription; a record that tried is
        # a different scene, not a longer one.
        driver, _, options = self._connect_failure(
            ledger=_connect_failure_ledger(extra=("gatt-read",))
        )
        with self.assertRaisesRegex(RuntimeError, "do not describe this scene"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_a_failure_the_record_does_not_identify_cannot_pass(self) -> None:
        driver, _, options = self._connect_failure(
            ledger=_connect_failure_ledger(detail="the connect failed")
        )
        with self.assertRaisesRegex(RuntimeError, "names no failure identity"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    def test_the_quiet_window_must_have_been_sampled_after_a_failure(self) -> None:
        driver, _, options = self._connect_failure()
        for row in driver.results["attempt"]["receipt"]["steps"]:
            row["observed"].pop("still", None)
        with self.assertRaisesRegex(RuntimeError, "quiet window"):
            driver.drive(CONNECT_FAILURE_SCENE, options)

    # -- the link-facts scene ------------------------------------------------------

    def _link_facts(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """Four link facts the platform answered, then bent by one fact."""
        driver = self._scripted(
            LINK_FACTS_SCENE,
            projections or _link_facts_projection(),
            ledgers={"facts": ledger if ledger is not None else _link_facts_ledger()},
            # Three disjunctions in this plan; the derivation reads the projection, so the
            # branch index only has to be a value the receipt can honestly carry.
            wait_any_for={"facts": 0},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_link_facts_are_answered_and_the_host_touches_nothing(self) -> None:
        driver, fixture, options = self._link_facts()
        driver.drive(LINK_FACTS_SCENE, options)
        self.assertEqual(options["observations"], list(LINK_FACTS_SCENE.trace))
        # Every one of these is answered by the platform on the live link: the peer is never
        # asked to do anything and the host never touches it.
        self.assertEqual(fixture.calls, [])

    def test_a_platform_refusal_is_an_answer_not_a_failure(self) -> None:
        # CoreBluetooth cannot request an MTU, set a priority or invalidate its cache. The
        # same trace must hold, because the claim is "the answer was surfaced", not "it worked".
        driver, _, options = self._link_facts(
            projections=_link_facts_projection(unsupported=True),
            ledger=_link_facts_ledger(unsupported=True),
        )
        driver.drive(LINK_FACTS_SCENE, options)
        self.assertEqual(options["observations"], list(LINK_FACTS_SCENE.trace))

    def test_an_unsupported_answer_must_carry_the_platform_code(self) -> None:
        projections = _link_facts_projection(unsupported=True)
        projections["facts"]["priority"] = _frame(
            state="started", link="connected", priority="unsupported", failure="none"
        )
        driver, _, options = self._link_facts(
            projections=projections, ledger=_link_facts_ledger(unsupported=True)
        )
        with self.assertRaisesRegex(RuntimeError, "only the platform's own refusal"):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_a_failed_link_fact_cannot_pass(self) -> None:
        projections = _link_facts_projection()
        projections["facts"]["refresh"] = _frame(
            state="started", link="connected", refresh="failed", failure="gatt/133"
        )
        driver, _, options = self._link_facts(projections=projections)
        with self.assertRaisesRegex(
            RuntimeError, "must settle or be refused as unsupported"
        ):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_a_pending_link_fact_cannot_pass(self) -> None:
        # A submission is not an answer: a lane left `pending` means the scene never learned
        # what the platform said, which is exactly the hang a timeout would hide.
        projections = _link_facts_projection()
        projections["facts"]["mtuRequest"] = _frame(
            state="started", link="connected", mtuRequest="pending"
        )
        driver, _, options = self._link_facts(projections=projections)
        with self.assertRaisesRegex(
            RuntimeError, "must settle or be refused as unsupported"
        ):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_an_mtu_answer_without_limits_cannot_pass(self) -> None:
        projections = _link_facts_projection()
        projections["facts"]["mtu"] = _frame(
            state="started", link="connected", mtu="done", mtuWrites="none/none"
        )
        driver, _, options = self._link_facts(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "unusable payload limit"):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_an_mtu_limit_below_the_att_floor_cannot_pass(self) -> None:
        projections = _link_facts_projection()
        projections["facts"]["mtu"] = _frame(
            state="started", link="connected", mtu="done", mtuWrites="19/19"
        )
        driver, _, options = self._link_facts(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "unusable payload limit"):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_a_recorded_failure_that_is_not_a_missing_capability_cannot_pass(
        self,
    ) -> None:
        driver, _, options = self._link_facts(
            ledger=_link_facts_ledger(
                unsupported=True, detail="observation/bleng.timeout: expired"
            )
        )
        with self.assertRaisesRegex(
            RuntimeError, "other than an unsupported capability"
        ):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_a_request_that_settled_must_show_up_in_the_re_query(self) -> None:
        # An accepted request is a receipt. The scene re-reads the limits so "applied" is an
        # observation: a settling request whose limits shrank cannot be that.
        projections = _link_facts_projection()
        projections["facts"]["mtu"] = _frame(
            state="started", link="connected", mtu="done", mtuWrites="244/241"
        )
        projections["facts"]["mtuAfter"] = _frame(
            state="started", link="connected", mtu="done", mtuWrites="244/200"
        )
        driver, _, options = self._link_facts(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "platform reports smaller limits"):
            driver.drive(LINK_FACTS_SCENE, options)

    def test_a_refused_request_must_not_change_the_limits(self) -> None:
        # The mirror: a platform that says it cannot do this must keep reporting what it had.
        projections = _link_facts_projection(unsupported=True)
        projections["facts"]["mtuAfter"] = _frame(
            state="started", link="connected", mtu="done", mtuWrites="244/244"
        )
        driver, _, options = self._link_facts(
            projections=projections, ledger=_link_facts_ledger(unsupported=True)
        )
        with self.assertRaisesRegex(RuntimeError, "limits changed"):
            driver.drive(LINK_FACTS_SCENE, options)

    # -- the CCCD scene -----------------------------------------------------------

    def _descriptor_io(
        self,
        *,
        mode: str = "notification",
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """A subscription whose peer CCCD agrees with it, then bent by one fact."""
        driver = self._scripted(
            DESCRIPTOR_IO_SCENE,
            projections or _descriptor_io_projection(mode),
            subscription={**SUBSCRIPTION, "mode": mode},
            gatt={"uuid": SUBSCRIPTION["uuid"]},
            ledgers={"descriptor": ledger or _ledger(*DESCRIPTOR_IO_LEDGER)},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_the_peers_descriptor_tracks_the_subscription_in_both_directions(
        self,
    ) -> None:
        driver, fixture, options = self._descriptor_io()
        driver.drive(DESCRIPTOR_IO_SCENE, options)
        self.assertEqual(options["observations"], list(DESCRIPTOR_IO_SCENE.trace))
        # Nothing here is the host's: every reading is the peer's own answer to a command
        # the App submitted, so the peer is never asked to act.
        self.assertEqual(fixture.calls, [])

    def test_a_read_retried_until_it_settled_is_still_one_command(self) -> None:
        # `tapUntil` re-taps whenever the condition is not yet visible, so the App can record
        # the same read twice for one command the host asked for. That is one command observed
        # until it settled, not two commands: the ledger folds adjacent `X, X-wait` repeats.
        doubled: list[str] = []
        for name in DESCRIPTOR_IO_LEDGER:
            doubled.append(name)
            if name == "descriptor-read-wait":
                doubled.extend(["descriptor-read", "descriptor-read-wait"])
        driver, _, options = self._descriptor_io(ledger=_ledger(*doubled))
        driver.drive(DESCRIPTOR_IO_SCENE, options)
        self.assertEqual(options["observations"], list(DESCRIPTOR_IO_SCENE.trace))

    def test_an_unchanged_read_that_never_happened_cannot_pass(self) -> None:
        # The scene's last act is a *fresh* read of the unchanged CCCD. A record that stops at
        # the write proves the write but not that the peer still reports the same bytes, so a
        # plan whose final read silently settled on the cleared read's value must fail here.
        truncated = list(DESCRIPTOR_IO_LEDGER)
        del truncated[-3:-1]
        driver, _, options = self._descriptor_io(ledger=_ledger(*truncated))
        with self.assertRaisesRegex(RuntimeError, "do not describe this scene"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    def test_a_record_the_app_truncated_cannot_be_read_as_the_whole_session(
        self,
    ) -> None:
        # The App's ledger is one bounded window. A scene that expects every action must fail
        # when the window dropped some, instead of comparing a suffix and passing on it.
        frame = _ledger(*DESCRIPTOR_IO_LEDGER).replace("omitted=0", "omitted=1", 1)
        driver, _, options = self._descriptor_io(ledger=frame)
        with self.assertRaisesRegex(RuntimeError, "bounded action window dropped"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    def test_the_enabled_bit_follows_the_declared_mode(self) -> None:
        # An indication subscription enables bit 1, not bit 0; a scene that hardcoded `01 00`
        # would pass on notification and lie on indication.
        driver, _, options = self._descriptor_io(
            mode="indication", projections=_descriptor_io_projection("indication")
        )
        driver.drive(DESCRIPTOR_IO_SCENE, options)
        self.assertEqual(options["observations"], list(DESCRIPTOR_IO_SCENE.trace))

    def test_a_peer_that_did_not_enable_its_cccd_cannot_pass(self) -> None:
        # The App says `subscribed`; the descriptor the peer stores says it never was. That
        # disagreement is exactly what a single-witness scene cannot see.
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-enabled"] = _frame(
            state="started", link="connected", descriptor="done", descriptorValue="0000"
        )
        driver, _, options = self._descriptor_io(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "descriptorValue is '0000'"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    def test_a_disable_the_peer_did_not_apply_cannot_pass(self) -> None:
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-cleared"] = _frame(
            state="started", link="connected", descriptor="done", descriptorValue="0100"
        )
        driver, _, options = self._descriptor_io(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "descriptorValue is '0100'"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    def test_a_platform_that_refuses_the_write_as_itself_still_passes(self) -> None:
        # CoreBluetooth refuses a direct CCCD write: the answer is the platform's own typed
        # refusal, and the bytes must still be the ones the disable left behind.
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-written"] = _frame(
            state="started",
            link="connected",
            descriptor="done",
            descriptorValue="0000",
            descriptorWrite="unsupported",
            failure="submission/" + UNSUPPORTED_CODE,
        )
        ledger = _ledger_from(
            [
                ("start", "succeeded", ""),
                ("begin-scan", "succeeded", ""),
                ("scan-start-wait", "succeeded", ""),
                ("scan-pull", "succeeded", ""),
                ("scan-stop", "succeeded", ""),
                ("connect", "succeeded", ""),
                ("connect-wait", "succeeded", ""),
                ("gatt-discover", "succeeded", ""),
                ("gatt-discover-wait", "succeeded", ""),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("notify-enable", "succeeded", ""),
                ("notify-enable-wait", "succeeded", ""),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("notify-disable", "succeeded", ""),
                ("notify-disable-wait", "succeeded", ""),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                (
                    "descriptor-write",
                    "failed",
                    UNSUPPORTED_CODE + ": use setNotifyValue",
                ),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("close", "succeeded", ""),
            ]
        )
        driver, _, options = self._descriptor_io(projections=projections, ledger=ledger)
        driver.drive(DESCRIPTOR_IO_SCENE, options)
        self.assertEqual(options["observations"], list(DESCRIPTOR_IO_SCENE.trace))

    def test_a_refusal_reported_for_the_queued_operation_also_passes(self) -> None:
        # The other boundary the same refusal can arrive at: the engine accepts the submission
        # and the backend's `not_supported` is the operation's terminal, so both rows exist.
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-written"] = _frame(
            state="started",
            link="connected",
            descriptor="done",
            descriptorValue="0000",
            descriptorWrite="unsupported",
            failure="operation/" + UNSUPPORTED_CODE,
        )
        ledger = _ledger_from(
            [
                ("start", "succeeded", ""),
                ("begin-scan", "succeeded", ""),
                ("scan-start-wait", "succeeded", ""),
                ("scan-pull", "succeeded", ""),
                ("scan-stop", "succeeded", ""),
                ("connect", "succeeded", ""),
                ("connect-wait", "succeeded", ""),
                ("gatt-discover", "succeeded", ""),
                ("gatt-discover-wait", "succeeded", ""),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("notify-enable", "succeeded", ""),
                ("notify-enable-wait", "succeeded", ""),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("notify-disable", "succeeded", ""),
                ("notify-disable-wait", "succeeded", ""),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("descriptor-write", "succeeded", ""),
                (
                    "descriptor-write-wait",
                    "failed",
                    UNSUPPORTED_CODE + ": use setNotifyValue",
                ),
                ("descriptor-read", "succeeded", ""),
                ("descriptor-read-wait", "succeeded", ""),
                ("close", "succeeded", ""),
            ]
        )
        driver, _, options = self._descriptor_io(projections=projections, ledger=ledger)
        driver.drive(DESCRIPTOR_IO_SCENE, options)
        self.assertEqual(options["observations"], list(DESCRIPTOR_IO_SCENE.trace))

    def test_an_unsupported_write_must_name_the_platforms_code(self) -> None:
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-written"] = _frame(
            state="started",
            link="connected",
            descriptor="done",
            descriptorValue="0000",
            descriptorWrite="unsupported",
            failure="operation/bleng.operation.gatt",
        )
        driver, _, options = self._descriptor_io(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "platform's own refusal"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    def test_a_write_that_never_settled_cannot_pass(self) -> None:
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-written"] = _frame(
            state="started",
            link="connected",
            descriptor="done",
            descriptorValue="0100",
            descriptorWrite="failed",
        )
        driver, _, options = self._descriptor_io(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "same-value descriptor write"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    def test_a_descriptor_read_that_never_settled_cannot_pass(self) -> None:
        projections = _descriptor_io_projection()
        projections["descriptor"]["cccd-initial"] = _frame(
            state="started", link="connected", descriptor="failed"
        )
        driver, _, options = self._descriptor_io(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "no descriptor value"):
            driver.drive(DESCRIPTOR_IO_SCENE, options)

    # -- the system inventory scene -------------------------------------------------

    def _devices_query(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """One live link the adapter reports as connected, then bent by one fact."""
        driver = self._scripted(
            DEVICES_QUERY_SCENE,
            projections or _devices_query_projection(),
            ledgers={"devices": ledger or _ledger(*DEVICES_QUERY_LEDGER)},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_the_inventory_contains_the_peer_this_run_linked_to(self) -> None:
        driver, fixture, options = self._devices_query()
        driver.drive(DEVICES_QUERY_SCENE, options)
        self.assertEqual(options["observations"], list(DEVICES_QUERY_SCENE.trace))
        # The inventory is the platform's, not the host's: nothing here acts on the peer.
        self.assertEqual(fixture.calls, [])

    def test_an_inventory_that_omits_the_linked_peer_cannot_pass(self) -> None:
        # A list came back, but it does not describe this link. `devicesPeer=no` is a real
        # answer, and a scene that accepted the count alone would hide the divergence.
        driver, _, options = self._devices_query(
            projections=_devices_query_projection(peer="no")
        )
        with self.assertRaisesRegex(RuntimeError, "does not contain the peer"):
            driver.drive(DEVICES_QUERY_SCENE, options)

    def test_an_empty_inventory_cannot_pass(self) -> None:
        driver, _, options = self._devices_query(
            projections=_devices_query_projection(count="0", peer="no")
        )
        with self.assertRaisesRegex(RuntimeError, "no usable entries"):
            driver.drive(DEVICES_QUERY_SCENE, options)

    def test_an_inventory_query_that_never_settled_cannot_pass(self) -> None:
        driver, _, options = self._devices_query(
            projections=_devices_query_projection(devices="failed")
        )
        with self.assertRaisesRegex(RuntimeError, "did not settle"):
            driver.drive(DEVICES_QUERY_SCENE, options)

    def test_an_inventory_record_must_name_the_query_it_made(self) -> None:
        missing = list(DEVICES_QUERY_LEDGER)
        missing.remove("devices-query-wait")
        driver, _, options = self._devices_query(ledger=_ledger(*missing))
        with self.assertRaisesRegex(RuntimeError, "do not describe this scene"):
            driver.drive(DEVICES_QUERY_SCENE, options)

    # -- the standing-request scene ------------------------------------------------

    def _auto_connect(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        driver = self._scripted(
            AUTO_CONNECT_SCENE,
            projections or _auto_connect_projection(),
            ledgers={"settle": ledger or _ledger(*AUTO_CONNECT_LEDGER)},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    @staticmethod
    def _refused_ledger(detail: str) -> str:
        """The record a platform without the capability leaves: the submission is refused."""
        return _ledger_from(
            [
                ("start", "succeeded", ""),
                ("begin-scan", "succeeded", ""),
                ("scan-start-wait", "succeeded", ""),
                ("scan-pull", "succeeded", ""),
                ("scan-stop", "succeeded", ""),
                ("auto-connect", "failed", detail),
                ("close", "succeeded", ""),
            ]
        )

    @staticmethod
    def _refused_at_wait_ledger(detail: str) -> str:
        """CoreBluetooth's shape: the submission is accepted and the operation is refused."""
        return _ledger_from(
            [
                ("start", "succeeded", ""),
                ("begin-scan", "succeeded", ""),
                ("scan-start-wait", "succeeded", ""),
                ("scan-pull", "succeeded", ""),
                ("scan-stop", "succeeded", ""),
                ("auto-connect", "succeeded", ""),
                ("auto-connect-wait", "failed", detail),
                ("close", "succeeded", ""),
            ]
        )

    def test_a_standing_request_the_platform_answers_links_after_the_peer_returns(
        self,
    ) -> None:
        driver, fixture, options = self._auto_connect()
        driver.drive(AUTO_CONNECT_SCENE, options)
        self.assertEqual(options["observations"], list(AUTO_CONNECT_SCENE.trace))
        # The peer's absence and return are the host's acts, in that order; the App is told
        # nothing, so a link can only appear because the platform was waiting. The scene also
        # declares that an immediate adoption is its expected answer, which is why the peer
        # records the stronger resume rather than the strict one.
        self.assertEqual(fixture.calls, ["withdraw", "resume:adoption-expected"])

    def test_a_platform_without_the_capability_refuses_it_as_itself(self) -> None:
        driver, _, options = self._auto_connect(
            projections=_auto_connect_projection(
                answer="unsupported",
                link="none",
                failure="submission/" + UNSUPPORTED_CODE,
            ),
            ledger=self._refused_ledger(UNSUPPORTED_CODE + ": no standing connect"),
        )
        driver.drive(AUTO_CONNECT_SCENE, options)
        self.assertEqual(options["observations"], list(AUTO_CONNECT_SCENE.trace))

    def test_a_standing_request_refused_at_the_wait_also_passes(self) -> None:
        # CoreBluetooth's shape: the submission is accepted and the operation's terminal is the
        # platform's own refusal. Both ledger rows exist, and that is one answer, not two.
        driver, _, options = self._auto_connect(
            projections=_auto_connect_projection(
                answer="unsupported",
                link="none",
                failure="operation/" + UNSUPPORTED_CODE,
            ),
            ledger=self._refused_at_wait_ledger(
                UNSUPPORTED_CODE + ": CoreBluetooth cannot request an auto-connect link"
            ),
        )
        driver.drive(AUTO_CONNECT_SCENE, options)
        self.assertEqual(options["observations"], list(AUTO_CONNECT_SCENE.trace))

    def test_an_unanswered_standing_request_cannot_pass(self) -> None:
        driver, _, options = self._auto_connect(
            projections=_auto_connect_projection(answer="pending", link="connecting")
        )
        with self.assertRaisesRegex(RuntimeError, "never answered"):
            driver.drive(AUTO_CONNECT_SCENE, options)

    def test_an_unsupported_answer_must_name_the_platform_code(self) -> None:
        driver, _, options = self._auto_connect(
            projections=_auto_connect_projection(
                answer="unsupported",
                link="none",
                failure="observation/bleng.timeout",
            ),
            ledger=self._refused_ledger(UNSUPPORTED_CODE + ": no standing connect"),
        )
        with self.assertRaisesRegex(RuntimeError, "platform's own refusal"):
            driver.drive(AUTO_CONNECT_SCENE, options)

    # -- the other inventories ------------------------------------------------------

    def _devices_kinds(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        driver = self._scripted(
            DEVICES_KINDS_SCENE,
            projections or _devices_kinds_projection(),
            ledgers={"kinds": ledger or _ledger(*DEVICES_KINDS_LEDGER)},
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    @staticmethod
    def _refused_kinds_ledger() -> str:
        """Both submissions refused, so no wait row exists and the two collapse onto each other."""
        return _ledger_from(
            [
                ("start", "succeeded", ""),
                ("devices-query", "failed", UNSUPPORTED_CODE + ": no such inventory"),
                ("devices-query", "failed", UNSUPPORTED_CODE + ": no such inventory"),
                ("close", "succeeded", ""),
            ]
        )

    def test_each_inventory_answers_with_the_kind_it_was_asked_for(self) -> None:
        driver, fixture, options = self._devices_kinds()
        driver.drive(DEVICES_KINDS_SCENE, options)
        self.assertEqual(options["observations"], list(DEVICES_KINDS_SCENE.trace))
        # No peer and no link: nothing here is the host's to do.
        self.assertEqual(fixture.calls, [])

    def test_a_platform_without_these_inventories_refuses_each_one(self) -> None:
        driver, _, options = self._devices_kinds(
            projections=_devices_kinds_projection(
                bonded="unsupported", companion="unsupported"
            ),
            ledger=self._refused_kinds_ledger(),
        )
        driver.drive(DEVICES_KINDS_SCENE, options)
        self.assertEqual(options["observations"], list(DEVICES_KINDS_SCENE.trace))

    def test_a_platform_may_answer_one_inventory_and_refuse_the_other(self) -> None:
        # An Android without companion associations answers `bonded` and refuses `companion`;
        # the ledger shape follows the answers instead of being pinned to "both or neither".
        ledger = _ledger_from(
            [
                ("start", "succeeded", ""),
                ("devices-query", "succeeded", ""),
                ("devices-query-wait", "succeeded", ""),
                (
                    "devices-query",
                    "failed",
                    UNSUPPORTED_CODE + ": no companion inventory",
                ),
                ("close", "succeeded", ""),
            ]
        )
        driver, _, options = self._devices_kinds(
            projections=_devices_kinds_projection(companion="unsupported"),
            ledger=ledger,
        )
        driver.drive(DEVICES_KINDS_SCENE, options)
        self.assertEqual(options["observations"], list(DEVICES_KINDS_SCENE.trace))

    def test_a_refused_inventory_must_be_the_platforms_own_refusal(self) -> None:
        projections = _devices_kinds_projection(companion="unsupported")
        projections["kinds"]["companion"] = _frame(
            state="started",
            devices="unsupported",
            devicesKind="companion",
            failure="observation/bleng.timeout",
        )
        driver, _, options = self._devices_kinds(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "platform's own refusal"):
            driver.drive(DEVICES_KINDS_SCENE, options)

    def test_an_inventory_reading_must_name_its_own_kind(self) -> None:
        projections = _devices_kinds_projection()
        projections["kinds"]["bonded"] = _frame(
            state="started", devices="done", devicesKind="companion", devicesCount="0"
        )
        driver, _, options = self._devices_kinds(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "answers a different inventory"):
            driver.drive(DEVICES_KINDS_SCENE, options)

    def test_an_unanswered_inventory_cannot_pass(self) -> None:
        projections = _devices_kinds_projection()
        projections["kinds"]["bonded"] = _frame(
            state="started", devices="pending", devicesKind="bonded"
        )
        driver, _, options = self._devices_kinds(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "never answered"):
            driver.drive(DEVICES_KINDS_SCENE, options)

    # -- the radio power scene -----------------------------------------------------

    def _adapter_control(
        self,
        *,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        driver = self._scripted(
            ADAPTER_CONTROL_SCENE,
            projections or _adapter_control_projection(),
            ledgers={
                # The default answer is the platform's refusal at the *submission* boundary, so
                # no wait row exists and the two refusals collapse onto one name.
                "radio": ledger
                or _ledger_from(
                    [
                        ("start", "succeeded", ""),
                        (
                            "adapter-control",
                            "failed",
                            UNSUPPORTED_CODE + ": cannot change the radio power state",
                        ),
                        (
                            "adapter-control",
                            "failed",
                            UNSUPPORTED_CODE + ": cannot change the radio power state",
                        ),
                        ("close", "succeeded", ""),
                    ]
                )
            },
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_both_radio_directions_answer_with_the_platforms_own_refusal(self) -> None:
        driver, fixture, options = self._adapter_control()
        driver.drive(ADAPTER_CONTROL_SCENE, options)
        self.assertEqual(options["observations"], list(ADAPTER_CONTROL_SCENE.trace))
        # No peer and no link: radio power is the host's to observe, never to cause.
        self.assertEqual(fixture.calls, [])

    def test_a_platform_that_really_changes_the_radio_also_passes(self) -> None:
        # A device/profile owner is exempt from the API 33+ refusal, so `done` is legitimate —
        # and then the App must not report any failure alongside it.
        driver, _, options = self._adapter_control(
            projections=_adapter_control_projection(off="done", on="done"),
            ledger=_ledger(
                "start",
                "adapter-control",
                "adapter-control-wait",
                "adapter-control",
                "adapter-control-wait",
                "close",
            ),
        )
        driver.drive(ADAPTER_CONTROL_SCENE, options)
        self.assertEqual(options["observations"], list(ADAPTER_CONTROL_SCENE.trace))

    def test_a_refusal_reported_at_the_operation_boundary_is_the_same_answer(
        self,
    ) -> None:
        # Android's adapter power request is answered asynchronously, so its `not_supported`
        # arrives as `bleng.operation.not_supported`. One answer, two composed shapes.
        projections = _adapter_control_projection()
        for direction in ("off", "on"):
            projections["radio"][direction] = _frame(
                state="started",
                scan="idle",
                radio="unsupported",
                radioTarget=direction,
                failure="operation/bleng.operation.not_supported",
            )
        ledger = _ledger_from(
            [
                ("start", "succeeded", ""),
                ("adapter-control", "succeeded", ""),
                (
                    "adapter-control-wait",
                    "failed",
                    "operation/bleng.operation.not_supported: Android refused",
                ),
                ("adapter-control", "succeeded", ""),
                (
                    "adapter-control-wait",
                    "failed",
                    "operation/bleng.operation.not_supported: Android refused",
                ),
                ("close", "succeeded", ""),
            ]
        )
        driver, _, options = self._adapter_control(
            projections=projections, ledger=ledger
        )
        driver.drive(ADAPTER_CONTROL_SCENE, options)
        self.assertEqual(options["observations"], list(ADAPTER_CONTROL_SCENE.trace))

    def test_a_radio_answer_must_name_its_own_direction(self) -> None:
        projections = _adapter_control_projection()
        projections["radio"]["off"] = _frame(
            state="started",
            radio="unsupported",
            radioTarget="on",
            failure="submission/" + UNSUPPORTED_CODE,
        )
        driver, _, options = self._adapter_control(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "answered a different direction"):
            driver.drive(ADAPTER_CONTROL_SCENE, options)

    def test_a_radio_request_that_was_never_answered_cannot_pass(self) -> None:
        projections = _adapter_control_projection()
        projections["radio"]["off"] = _frame(
            state="started", radio="pending", radioTarget="off"
        )
        driver, _, options = self._adapter_control(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "never answered"):
            driver.drive(ADAPTER_CONTROL_SCENE, options)

    def test_an_untyped_radio_refusal_cannot_pass(self) -> None:
        projections = _adapter_control_projection()
        projections["radio"]["on"] = _frame(
            state="started",
            radio="unsupported",
            radioTarget="on",
            failure="observation/bleng.timeout",
        )
        driver, _, options = self._adapter_control(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "platform's own refusal"):
            driver.drive(ADAPTER_CONTROL_SCENE, options)

    def test_a_radio_request_may_not_disturb_the_main_line(self) -> None:
        projections = _adapter_control_projection()
        projections["radio"]["on"] = _frame(
            state="started",
            radio="unsupported",
            radioTarget="on",
            failure="submission/" + UNSUPPORTED_CODE,
            scan="running",
        )
        driver, _, options = self._adapter_control(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "scan is 'running'"):
            driver.drive(ADAPTER_CONTROL_SCENE, options)

    # -- the repeated peer ends scene ---------------------------------------------

    def _recovery(
        self,
        *,
        cycles: int | None = None,
        projections: dict[str, dict] | None = None,
        ledger: str | None = None,
    ) -> tuple[_ScriptedDriver, _Peer, dict[str, Any]]:
        """N peer ends answered correctly, then bent by one fact."""
        cycles = RECOVERY_SCENE.cycles if cycles is None else cycles
        if cycles is None:
            raise AssertionError("the recovery scene declares no cycles")
        session = ledger if ledger is not None else _recovery_ledger(cycles)
        driver = self._scripted(
            RECOVERY_SCENE,
            projections or _recovery_projection(cycles),
            gatt={"uuid": IO["uuid"]},
            # The App's log is one bounded window over the whole session, so every phase is
            # handed it; only the phases that actually read one keep it.
            ledgers={f"announce:{index}": session for index in range(cycles)}
            | {"scan:0": session},
            values={
                f"scan:{index}": {f"cycle-{index}-value": f"{IO['uuid']}: 1 bytes [00]"}
                for index in range(cycles)
            },
        )
        fixture = _Peer()
        options = self._options()
        options.update(fixture=fixture)
        return driver, fixture, options

    def test_a_recovery_repeats_the_cycle_on_one_owner(self) -> None:
        cycles = RECOVERY_SCENE.cycles or 0
        driver, fixture, options = self._recovery()
        driver.drive(RECOVERY_SCENE, options)
        self.assertEqual(options["observations"], list(RECOVERY_SCENE.trace))
        # One drop per cycle, and the peer offers itself again only between cycles: that
        # alternation is the scene, not an optimisation of it.
        self.assertEqual(fixture.calls, ["drop", "resume"] * (cycles - 1) + ["drop"])
        record = json.loads(
            (self.last_output / "record.json").read_text(encoding="utf-8")
        )
        hosts = [phase["host"] for phase in record["phases"]]
        self.assertEqual(len(hosts), cycles * 2)
        self.assertIsNone(hosts[0], "the first cycle opens the session")
        self.assertEqual(hosts[1::2], ["drop"] * cycles)
        self.assertEqual(hosts[2::2], ["resume"] * (cycles - 1))

    def test_a_cycle_whose_own_read_never_settled_cannot_pass(self) -> None:
        # A scan is a run in the App, so each cycle's read is that run's first settled read.
        # A zero here means the cycle reached the peer but read it from an older run.
        projections = _recovery_projection(RECOVERY_SCENE.cycles or 0)
        projections["scan:1"]["cycle-1-served"] = _frame(
            state="started",
            scan="stopped",
            link="connected",
            gatt="ready",
            gattMatch="one",
            gattRead="done",
            gattReadTotal="0",
        )
        driver, _, options = self._recovery(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "gattReadTotal is '0'"):
            driver.drive(RECOVERY_SCENE, options)

    def test_a_recovery_cycle_that_blames_nobody_cannot_pass(self) -> None:
        projections = _recovery_projection(RECOVERY_SCENE.cycles or 0)
        projections["announce:1"]["cycle-1-announced"] = _frame(
            state="started", link="disconnected", linkEnd="requested"
        )
        driver, _, options = self._recovery(projections=projections)
        with self.assertRaisesRegex(RuntimeError, "linkEnd is 'requested'"):
            driver.drive(RECOVERY_SCENE, options)

    def test_a_recovery_must_not_have_learned_an_end_from_a_failed_read(self) -> None:
        driver, _, options = self._recovery(
            ledger=_recovery_ledger(RECOVERY_SCENE.cycles or 0, read_wait="failed")
        )
        with self.assertRaisesRegex(RuntimeError, "recorded a failed action"):
            driver.drive(RECOVERY_SCENE, options)

    # -- the background scene ----------------------------------------------------

    def _phase_view(
        self,
        driver: _InProcessDriver,
        name: str,
        projection: dict,
        *,
        advertised: str = ADVERTISED,
        ledger: str | None = None,
        values: dict | None = None,
        held: float | None = None,
        wait_any: int | None = None,
        status: str = "passed",
    ) -> dict:
        """One phase exactly as a driver publishes it: its own rows and its own reads."""
        phase = next(item for item in driver._phases if item.name == name)

        def observed_for(step: dict) -> dict:
            # A pull loop is where an advertisement is seen, so the fake puts one there: the
            # derivation reads the peer's name out of that step's observation. A background
            # step reports how long the App stayed away — that sample is the temporal claim.
            # A disjunction reports which of its conditions held; the host decides whether
            # that answer is acceptable, so the branch is the fake's to choose.
            if step.get("op") == "tapUntil":
                return {"scan-latest": advertised}
            if step.get("op") == "background":
                window = step["for"] if held is None else held
                return {"background": f"not-foreground for={float(window):.1f}s"}
            if step.get("op") == "waitAny" and wait_any is not None:
                return {"waitAny": str(wait_any)}
            if step.get("op") == "still":
                # A quiet window is a temporal claim: the receipt has to say it was sampled,
                # and for how long, or the derivation refuses it.
                return {"still": f"{step['for']}s"}
            return {}

        steps = [
            {
                "index": index,
                "step": step,
                "observed": observed_for(step),
                "seconds": 0.0,
            }
            for index, step in enumerate(driver._steps(phase))
        ]
        reads = dict(values or {})
        if ledger is not None:
            reads["ledger"] = ledger
        # A real lane publishes only the reads its plan asked for, so a fake that handed over
        # every value would let a derivation pass on a read the plan never performed. The
        # phase's own plan is therefore what decides which values exist here.
        wanted = {
            step["as"]
            for step in driver._steps(phase)
            if step.get("op") == "read" and type(step.get("as")) is str
        }
        reads = {name: value for name, value in reads.items() if name in wanted}
        return {
            "status": status,
            "primary_error": "" if status == "passed" else "the step failed",
            "projection": projection,
            "receipt": {
                "status": status,
                "primary_error": "" if status == "passed" else "the step failed",
                "steps": steps,
                "values": reads,
            },
        }

    def _scripted(
        self,
        scene,
        projections: dict[str, dict],
        *,
        ledgers: dict[str, str] | None = None,
        values: dict[str, dict] | None = None,
        gatt: dict[str, str] | None = None,
        subscription: dict[str, str] | None = None,
        wait_any_for: dict[str, int] | None = None,
        advertised: str = ADVERTISED,
    ) -> _ScriptedDriver:
        """A scene whose every phase the App answered as the given projections say."""
        extra: dict[str, Any] = {}
        if scene.kind in SUBSCRIPTION_KINDS:
            extra["subscription"] = subscription or SUBSCRIPTION
        if gatt is not None:
            extra["gatt"] = gatt
        # One drive publishes into one directory, so a test that drives twice (a refusal and
        # an acceptance, or two capabilities) needs two of them.
        output = self.root / f"{scene.name}-{uuid.uuid4()}" / "driver"
        self.last_output = output
        driver = _ScriptedDriver(
            scene=scene,
            device=DEVICE,
            app_id="test.bundle",
            output=output,
            cwd=self.root,
            expected_local_name=LOCAL_NAME,
            results={},
            **extra,
        )
        driver.results = {
            phase.name: self._phase_view(
                driver,
                phase.name,
                projections[phase.name],
                advertised=advertised,
                ledger=(ledgers or {}).get(phase.name),
                values=(values or {}).get(phase.name),
                wait_any=(wait_any_for or {}).get(phase.name),
            )
            for phase in driver._phases
        }
        return self._ready(driver)

    def _background(
        self,
        *,
        values_after_resume: str = "2",
        away: str = "passed",
        held: float | None = None,
    ) -> tuple[_InProcessDriver, dict[str, dict]]:
        """A background/foreground cycle the App answered correctly, then bent by one fact."""
        driver = self._driver(scene=BACKGROUND_SCENE)
        per_phase = {
            "open": self._phase_view(
                driver,
                "open",
                {
                    "initial": _frame(state="idle", scan="idle"),
                    "started": _frame(state="started"),
                    "scanning": _frame(
                        state="started",
                        scan="running",
                        scanTotal="2",
                        scanFilter=SUBSCRIPTION["uuid"],
                        failure="none",
                    ),
                    "stopped": _frame(scan="stopped"),
                    "linked": _frame(link="connected"),
                    "topology": _frame(gatt="ready"),
                },
            ),
            "subscribe": self._phase_view(
                driver,
                "subscribe",
                {
                    "subscribed": _frame(
                        notify="subscribed", notifyMode=SUBSCRIPTION["mode"]
                    )
                },
            ),
            "value": self._phase_view(
                driver, "value", {"notified": _frame(notifyTotal="1")}
            ),
            "away": {
                **self._phase_view(driver, "away", {}, held=held),
                "status": away,
            },
            "back": self._phase_view(
                driver,
                "back",
                {
                    "resumed": _frame(link="connected", notify="subscribed"),
                    "drained": _frame(notifyTotal=values_after_resume),
                    "closed": _frame(state="closed"),
                },
            ),
        }
        return driver, per_phase

    def test_a_subscription_that_outlived_the_background_window_is_the_whole_trace(
        self,
    ) -> None:
        # The scene's claim is an ordering, not a set of facts: the second value can only be
        # there because the subscription was still alive while the App was away.
        driver, per_phase = self._background()
        self.assertEqual(driver._observations(per_phase), list(BACKGROUND_SCENE.trace))

    def test_a_value_the_background_window_lost_cannot_pass(self) -> None:
        # One value after resuming means the window lost the second one, whatever the App's
        # link and subscription labels still say: "connected" is not "it kept delivering".
        driver, per_phase = self._background(values_after_resume="1")
        with self.assertRaisesRegex(
            RuntimeError, "did not outlive the background window"
        ):
            driver._observations(per_phase)

    def test_a_window_the_app_did_not_stay_away_for_cannot_pass(self) -> None:
        # The `away` phase is the fact, so a phase that did not pass cannot be narrated as one
        # that did — even when every later observation looks right.
        driver, per_phase = self._background(away="failed")
        with self.assertRaisesRegex(RuntimeError, "background phase did not pass"):
            driver._observations(per_phase)

    def test_a_window_the_receipt_does_not_show_cannot_pass(self) -> None:
        # A driver whose background step stopped holding the window would still report every
        # later fact correctly. The receipt has to show the dwell it claims, the same way the
        # passive-disconnect scene refuses a quiet window that was never sampled.
        driver, per_phase = self._background(held=12.0)
        with self.assertRaisesRegex(
            RuntimeError, "of the declared 30s background window"
        ):
            driver._observations(per_phase)

    def test_a_background_step_without_a_sample_cannot_pass(self) -> None:
        driver, per_phase = self._background()
        for row in per_phase["away"]["receipt"]["steps"]:
            row["observed"].pop("background", None)
        with self.assertRaisesRegex(RuntimeError, "was not sampled"):
            driver._observations(per_phase)

    def test_the_background_step_declares_the_window_it_holds(self) -> None:
        # C8 stated for a platform wait: the App is put away for `for` seconds, so the plan
        # states that window and both drivers refuse a step that leaves it out. An undeclared
        # hold would be a driver's own idea of how long "in the background" means.
        driver = self._driver(scene=BACKGROUND_SCENE)
        (away,) = driver._steps(Phase("away"))
        self.assertEqual(away["op"], "background")
        self.assertIsInstance(away["for"], int)
        self.assertGreater(away["for"], 0, away)

    def test_returning_waits_for_the_away_value_instead_of_reading_the_old_one(
        self,
    ) -> None:
        # The pull's wait is satisfied by the value that was already on screen before the App
        # left, so the plan must wait for the App's own counter instead of reading a frame the
        # previous cycle already explains. Measured on 2026-11-26: `drained` read
        # `notifyTotal=1` and `closed` read 2 a moment later, which the derivation correctly
        # refused as "the value emitted while the App was away never arrived".
        driver = self._driver(scene=BACKGROUND_SCENE)
        steps = driver._steps(Phase("back"))
        drained = next(
            index for index, step in enumerate(steps) if step.get("as") == "drained"
        )
        counter = next(
            index
            for index, step in enumerate(steps[:drained])
            if step.get("op") == "wait"
            and (step.get("status") or {}).get("notifyTotal") == "2"
        )
        self.assertLess(counter, drained)


if __name__ == "__main__":
    unittest.main()
