"""Drive one managed scene in-process, on either platform, from the same phases.

This is the native implementation of `SceneDriver` (see `bleng_scan_runner`). It owns exactly
one thing: running host-owned plans against the App the host already installed, launched and
pinned, and turning the drivers' receipts into the scene's observation trace.

Everything that describes the *scene* lives here — the phase list, the peer hold spec, the
ledgers and the eleven observation derivations — because none of it depends on which platform
invented the steps. A platform subclasses this module's `NativeSceneDriver` and supplies only
the parts that are genuinely about its own executor: what artifact it runs, how it binds the
App, how one phase is launched, and the driver record's kind and scope.

**A scene is a sequence of plans, not one plan.** That is a fact about the firmware, not a
style choice: a notification can only be delivered after the peer's CCC is enabled, so the
host has to act between the App's subscribe and its pull. In this lane the host does not own
the UI loop, so the boundary is a phase boundary: run the subscribe plan, let the host emit,
run the collect plan. Each phase is one run of the platform's driver against the same pinned
process (attach mode is what makes that safe — the App is never relaunched), and the trace is
derived across the phases.

The scene names it can drive are the ones with a phase list here. Adding one is a plan plus a
trace derivation.

Driver record (`driver/record.json`) states what actually ran: the artifact, the phases, the
per-phase receipts and timings. That replaces the Appium session record for this lane, and the
bundle verifier reads it by kind.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tests.peripherals.nrf52840.fault_control import REJECT_ATT_RESULT, Operation

from . import action_ledger
from .bleng_scan_evidence import (
    GATT_VALUE_TEXT,
    SceneEvidence,
    att_status_token,
    render_gatt_value,
)
from .capture_io import publish_new_json
from .peer_hold import PeerHold
from .scene_plans import CCC_BITS, CONTROLS_REQUIRING_LINK, handoff_step, plans
from .scene_receipt import decode_projection


@dataclass(frozen=True)
class Phase:
    """One run of the App, and the host action that must precede it.

    `name` is both the record key and the plan's identity (`<scene>.<phase>`, except the
    single-phase scan scene). `host` names an action the host performs between phases; the
    driver performs it under the envelope's own ownership guard, so the fixture is only
    touched while this run still owns the App and the phone.
    """

    name: str
    host: str | None = None


PHASES: dict[str, tuple[Phase, ...]] = {
    "scan": (Phase("scan"),),
    "io": (Phase("io"),),
    "notification": (Phase("subscribe"), Phase("collect", host="publish")),
    "indication": (Phase("subscribe"), Phase("collect", host="publish")),
    # A held read, a queued read cancelled behind it, and the read that completes after the
    # peer releases the hold are separate runs of the App because the *host* has to act
    # between them. The first phase only opens the link: the fixture refuses to hold a read
    # before it has a connected peer, so arming cannot precede the connection.
    "cancellation": (
        Phase("open"),
        Phase("hold", host="hold-arm"),
        Phase("queue", host="hold-verify"),
        Phase("settle", host="hold-release"),
    ),
    # The peer parks the exact write and then refuses it: two host actions (arm, refuse) and
    # three runs of the App — state the value, dispatch the parked write, then observe the
    # refusal and the recovery.
    "write-reject": (
        Phase("open"),
        Phase("dispatch", host="hold-arm"),
        Phase("settle", host="hold-reject"),
    ),
    # The peer parks the subscription's own CCC write and refuses it: one enable, one retry,
    # then the value the host emits is the only proof the recovered lane works.
    "ccc-enable-reject": (
        Phase("open"),
        Phase("enable", host="hold-arm"),
        Phase("settle", host="hold-reject"),
        Phase("collect", host="publish"),
    ),
    # The mirror: the peer refuses the *disable*, so the App must not claim the subscription
    # ended; one probe decides whether this platform still delivers, then a real disable.
    # The peer ends the link itself: one App run proves the link works, the host drops it, the
    # next run only observes what the App announced by itself.
    "passive-disconnect": (
        Phase("open"),
        Phase("ended", host="drop"),
    ),
    "ccc-disable-reject": (
        Phase("open"),
        Phase("subscribe"),
        # The host emits between the two: a value can only reach an established subscription.
        Phase("value", host="publish"),
        Phase("hold", host="hold-arm"),
        Phase("probe", host="hold-reject"),
    ),
    # The App's own end of a link it proved it could carry: the host emits once so the
    # subscription is a live lane rather than a stored phase, then the operator's command
    # ends the link and everything the link owned with it.
    "disconnect": (
        Phase("open"),
        Phase("value", host="publish"),
        Phase("sever"),
    ),
    # A connect the peer cannot answer. The host's withdrawal is between the two runs of the
    # App: the scan must have matched first (the App needs an advertisement to address), and
    # the withdrawal must be proved before the attempt, or the attempt measures the peer's
    # presence instead of its absence.
    "connect-failure": (
        Phase("open"),
        Phase("attempt", host="withdraw"),
    ),
    # Facts about the link itself. No host action and no GATT target: every fact is answered
    # by the platform, and the scene asserts the answer's *shape*, not its value.
    "link-facts": (Phase("facts"),),
    # The peer's descriptor state across a subscription's whole life. One process, no host
    # action: every step is a command the App submits and a reading it publishes.
    "descriptor-io": (Phase("descriptor"),),
    # The platform's own inventory across a live link. No host action and no GATT target:
    # the answer is a fact about the adapter, and the link is what makes it checkable.
    "devices-query": (Phase("devices"),),
    # The inventories the observation main line does not need: `bonded` and `companion`. They
    # are adapter facts, so this scene needs neither a peer nor a link — only the owner.
    "devices-kinds": (Phase("kinds"),),
    # Radio power, both directions. An adapter fact like the inventories above: no peer,
    # no link, no scan — only the owner and the platform's own answer.
    "adapter-control": (Phase("radio"),),
    # A standing "connect when the peer appears" request. The peer is withdrawn *before* the
    # request, so a link cannot be the plain connect path: it can only appear because the
    # platform was waiting while the peer was gone. The host restores the peer between phases.
    "auto-connect": (
        Phase("open"),
        Phase("arm", host="withdraw"),
        Phase("settle", host="resume"),
    ),
    # The link and its subscription must outlive a background/foreground cycle: the App is
    # sent away, the host emits while it is away, and the value is only observable after it
    # returns. That ordering is the whole claim, so each part is its own phase.
    "background": (
        Phase("open"),
        Phase("subscribe"),
        Phase("value", host="publish"),
        Phase("away"),
        Phase("back", host="publish"),
    ),
}
# A repeating scene states its own phase list: one scan phase and one announce phase per
# cycle, with the host's drop between them and its resume before the next cycle. Deriving it
# from `scene.cycles` is what keeps the phase list and the trace from disagreeing.
REPEATING_SCENES = frozenset({"recovery", "recovery-ten"})


def phases_for(scene: SceneEvidence) -> tuple[Phase, ...]:
    """The phase list one scene runs, static or derived from its declared cycle count."""
    if scene.kind in REPEATING_SCENES:
        cycles = scene.cycles
        if type(cycles) is not int or not 1 <= cycles <= 10:
            raise ValueError("a repeating scene must declare 1..10 cycles")
        phases: list[Phase] = []
        for index in range(cycles):
            if index == 0:
                phases.append(Phase(f"scan:{index}"))
            else:
                phases.append(Phase(f"scan:{index}", host="resume"))
            phases.append(Phase(f"announce:{index}", host="drop"))
        return tuple(phases)
    if scene.name not in PHASES:
        raise ValueError(
            f"the {scene.name} scene has no native plan yet; "
            f"planned: {sorted(PLANNED_SCENES)}"
        )
    return PHASES[scene.name]


PLANNED_SCENES = frozenset(PHASES) | REPEATING_SCENES
# What each scene kind must be handed about its GATT target. The lane never chooses a
# characteristic or a payload: the producer derives both from the fixture profile, and a key
# a scene does not use is refused rather than ignored.
# What each fault scene parks inside the firmware. A scene states its own operation and
# payload: a refused write parks a response write carrying the bytes it will ask for, and a
# cancellation parks a read.


def _ccc_request(mode: str, *, enabled: bool) -> bytes:
    if mode not in CCC_BITS:
        raise ValueError(f"unsupported notification mode {mode!r}")
    return CCC_BITS[mode] if enabled else bytes(2)


def _peer_hold_spec(
    kind: str,
    gatt: dict[str, str] | None,
    subscription: dict[str, str] | None,
) -> tuple[Operation, bytes, bytes | None]:
    """Which operation one fault scene parks, and what request it carries."""
    if kind == "cancellation":
        return (Operation.READ, b"", None)
    if kind == "write-reject":
        if gatt is None:
            raise ValueError("a refused write needs the declared payload")
        return (Operation.RESPONSE_WRITE, gatt["write"].encode("ascii"), None)
    if kind in {"ccc-enable-reject", "ccc-disable-reject"}:
        if subscription is None:
            raise ValueError(f"{kind} needs the profile-derived subscription target")
        mode = subscription["mode"]
        if kind == "ccc-enable-reject":
            return (Operation.CCC_WRITE, _ccc_request(mode, enabled=True), None)
        # The pre-state is the enable this characteristic's mode means, not the
        # descriptor's stored default: the managed CCC getter reports that default even
        # while the central is subscribed. The firmware re-checks it at capture, so a wrong
        # claim is a hold that never holds rather than a pass.
        return (
            Operation.CCC_WRITE,
            bytes(2),
            _ccc_request(mode, enabled=True),
        )
    raise ValueError(f"no peer hold is declared for a {kind!r} scene")


# Which scenes need a subscription target (`uuid`, `mode`, `value`). Stated rather than
# inferred from a phase name: a CCC scene happens to have a `collect` phase and a read/write
# scene does not, and a scene's target must never depend on that kind of coincidence.
SUBSCRIPTION_KINDS = frozenset(
    {
        "subscription",
        "ccc-enable-reject",
        "ccc-disable-reject",
        "background",
        "disconnect",
        # It subscribes to correlate the peer's CCCD with its own phase, and reads that CCCD
        # as a descriptor, so it needs both the subscription target and the read target.
        "descriptor-io",
    }
)
GATT_TARGET_KEYS: dict[str, frozenset[str]] = {
    "io": frozenset({"uuid", "write"}),
    "cancellation": frozenset({"uuid"}),
    "write-reject": frozenset({"uuid", "write"}),
    "passive-disconnect": frozenset({"uuid"}),
    "recovery": frozenset({"uuid"}),
    # A subscription scene that also reads: the read names the same characteristic the
    # subscription was established on, so one profile-declared target serves both.
    "disconnect": frozenset({"uuid"}),
    "descriptor-io": frozenset({"uuid"}),
}
# A subscription scene's ledger is the App's own ordered record. Repeats (pulls) are
# collapsed before comparison, because how many pulls a scan needs is a property of the RF
# environment, not of the scene.
SCAN_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "close",
)
SUBSCRIPTION_LEDGER = (
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
    "notify-disable-wait",
    "close",
)
# The read/write scene is the same prefix, then two reads separated by the write that must
# change what the second one observes. Repeats are collapsed like every other scene's, so
# this states the order the App's own record must show, not how many polls it took.
IO_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "gatt-discover",
    "gatt-discover-wait",
    "gatt-read",
    "gatt-read-wait",
    "gatt-write",
    "gatt-write-wait",
    "gatt-read",
    "gatt-read-wait",
    "rssi",
    "close",
)
# The cancellation scene's ledger has one *failed* wait by design (the cancelled read), so it
# cannot use a fixed all-succeeded template. It states the prefix every read/write scene
# shares, then whatever the two settled reads and the recovery read appended.
CANCELLATION_LEDGER_PREFIX = IO_LEDGER[:9]
# The read side of that record: the held read, the queued read (cancelled) and the recovery
# read, then the close. The cancel receipt and the cancelled wait settle independently, so
# both orders are this scene.
_CANCELLATION_TAIL = (
    "gatt-read",
    "gatt-read-wait",
    "gatt-read",
    "gatt-read-wait",
    "gatt-read-cancel",
    "gatt-read",
    "gatt-read-wait",
    "close",
)
_CANCELLATION_TAIL_SWAPPED = (
    "gatt-read",
    "gatt-read-wait",
    "gatt-read",
    "gatt-read-cancel",
    "gatt-read-wait",
    "gatt-read",
    "gatt-read-wait",
    "close",
)
CANCELLATION_LEDGER_TAILS = frozenset({_CANCELLATION_TAIL, _CANCELLATION_TAIL_SWAPPED})
CANCELLATION_READ_ACTIONS = frozenset(
    {"gatt-read", "gatt-read-wait", "gatt-read-cancel"}
)
# The write-reject scene's record: the same prefix, then a read, a refused write whose wait
# is the one failed action, a read that proves nothing moved, and a write that succeeds.
# One cycle's record: the scan, the link, one read and the end. A repeating scene asserts
# this for the cycle its bounded window still contains in full.
_RECOVERY_CYCLE_LEDGER = (
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "gatt-discover",
    "gatt-discover-wait",
    "gatt-read",
    "gatt-read-wait",
    "close",
)
# The peer's end is observed, not commanded: the record is the preamble, the read that proved
# the link worked, and the close — nothing else may appear in it.
_PASSIVE_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "gatt-discover",
    "gatt-discover-wait",
    "gatt-read",
    "gatt-read-wait",
    "close",
)
WRITE_REJECT_LEDGER = CANCELLATION_LEDGER_PREFIX + (
    "gatt-read",
    "gatt-read-wait",
    "gatt-write",
    "gatt-write-wait",
    "gatt-read",
    "gatt-read-wait",
    "gatt-write",
    "gatt-write-wait",
    "gatt-read",
    "gatt-read-wait",
    "close",
)
# The operator's own end: the read proving the link worked, the subscription that made the
# lane a fact, the one emitted value, and then the command this scene itself submitted. The
# ledger is the second witness that the end was a command, not an observation — a peer-initiated
# end has no `disconnect` entry at all (see `_PASSIVE_LEDGER`).
DISCONNECT_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "gatt-discover",
    "gatt-discover-wait",
    "gatt-read",
    "gatt-read-wait",
    "notify-enable",
    "notify-enable-wait",
    "notify-pull",
    "disconnect",
    "disconnect-wait",
    "close",
)
# The connect the peer cannot answer: the App's own record is the preamble, a scan that
# reached the peer, one accepted submission, one failed wait — and nothing else. The attempt
# is only a submission: no discovery, read, write or subscription may follow it.
CONNECT_FAILURE_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "close",
)
# Facts about the link, in the order the scene asks for them: query before request (the
# baseline a request must move), and the cache refresh last, because invalidating the topology
# is the one fact that can retire the platform's own GATT handles.
LINK_FACTS_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "mtu-query",
    "mtu-query-wait",
    "mtu-request",
    "mtu-request-wait",
    # The same query again: the second read is the request's *effect*, not its receipt.
    "mtu-query",
    "mtu-query-wait",
    "priority",
    "priority-wait",
    "refresh",
    "refresh-wait",
    "close",
)
# The CCCD scene's record: four descriptor reads, each placed where it proves something.
# The order is the plan's order, not a convenient one: read the cleared CCCD *before* the
# direct write (so a cleared value cannot be our own write's doing), and read it again after
# the write (so "unchanged" is a fresh read, not a carried-over value).
DESCRIPTOR_IO_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "gatt-discover",
    "gatt-discover-wait",
    "descriptor-read",
    "descriptor-read-wait",
    "notify-enable",
    "notify-enable-wait",
    "descriptor-read",
    "descriptor-read-wait",
    "notify-disable",
    "notify-disable-wait",
    "descriptor-read",
    "descriptor-read-wait",
    "descriptor-write",
    "descriptor-write-wait",
    "descriptor-read",
    "descriptor-read-wait",
    "close",
)
# The inventory scene's record: one link, then one query and its wait. No GATT step exists
# because the inventory is a fact about the adapter, not the topology.
DEVICES_QUERY_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "connect",
    "connect-wait",
    "devices-query",
    "devices-query-wait",
    "close",
)
# The standing-request scene: no link command at all, because the platform owns the wait. The
# wait row is Android's; a platform that cannot express the capability records only the refused
# submission, so the ledger check accepts that one shape explicitly instead of a second template.
AUTO_CONNECT_LEDGER = (
    "start",
    "begin-scan",
    "scan-start-wait",
    "scan-pull",
    "scan-stop",
    "auto-connect",
    "auto-connect-wait",
    "close",
)
# The other inventories' record: two queries, each with its wait, and nothing else. The two
# `devices-query` pairs are consecutive, so the ledger check folds them exactly like a retry;
# which kinds were asked for is stated by the projection reads, not by the action name.
# Two inventories, each with its own submission and wait. `_collapse` folds only *consecutive*
# repeats, so the two query pairs stay four names.
DEVICES_KINDS_LEDGER = (
    "start",
    "devices-query",
    "devices-query-wait",
    "devices-query",
    "devices-query-wait",
    "close",
)
# A platform's own "this capability does not exist here", as the supervisor composes it.
UNSUPPORTED_CODE = "bleng.dispatch_rejected.not_supported"


def is_not_supported(value: str) -> bool:
    """Whether a failure identity or ledger detail names the platform's own `not_supported`.

    The supervisor composes the code as `<path>.not_supported`: `bleng.dispatch_rejected.…` when
    the engine refuses a submission, and `bleng.operation.…` when the backend reports the refusal
    as the operation's own terminal (Android's adapter power request takes that path). One answer,
    two shapes — so this checks the terminal segment of the *code*, never the message, and never
    one full string: matching that single string judged a correct platform refusal as a failure
    (measured on `adapter-control`, 2026-11-26).
    """
    code = value.split(":", 1)[0].strip()
    return code == "not_supported" or code.endswith(".not_supported")


_MTU_LIMITS = re.compile(r"(?:[0-9]+|none)/(?:[0-9]+|none)")


_UNSAFE = re.compile(r"[^a-z0-9]+")
# The two shapes `runStatus.ts` reads out of a failure sentence. Kept next to the reader
# below rather than beside the token reader a peer's end uses: that one answers "which
# status did the platform report", this one answers "what does the projection render".
_RECORDED_PLATFORM = re.compile(r"\[([^/\]\s:]+)/([^\]\s:]+)")
_RECORDED_DOMAIN = re.compile(r"^([a-z]+)/([a-z0-9_.]+)")


def _slug(value: str) -> str:
    return _UNSAFE.sub("-", value.casefold()).strip("-")[:48] or "attachment"


def _collapse(actions: list[tuple[str, str]]) -> list[str]:
    """The ordered action names, with consecutive repeats folded to one."""
    names: list[str] = []
    for action, _ in actions:
        if not names or names[-1] != action:
            names.append(action)
    return names


def _collapse_retries(names: list[str]) -> list[str]:
    """Fold a command's repeated `X, X-wait` pairs into one logical command.

    A `tapUntil` step re-taps its control whenever the condition is not yet visible, so the
    App can legitimately record `read, read-wait, read, read-wait` for *one* command the host
    asked for. That is one command observed until it settled, not two commands: how many taps
    the RF and the platform's own cache needed is a property of the run, not of the scene.
    The fold is deliberately narrow — only a name followed by its own `-wait` — so an
    unrelated pair of same-named commands can never be merged.
    """
    folded = list(names)
    changed = True
    while changed and len(folded) >= 4:
        changed = False
        for index in range(len(folded) - 3):
            action, wait = folded[index], folded[index + 1]
            if (
                wait == f"{action}-wait"
                and folded[index + 2] == action
                and folded[index + 3] == wait
            ):
                del folded[index + 2 : index + 4]
                changed = True
                break
    return folded


def _recorded_failure_identity(detail: str) -> str | None:
    """The `run-status.failure` token one recorded failure must render.

    The App's record keeps the whole sentence (`domain/code: message [platform/code …]`) and
    its projection keeps only the identity, rendered exactly as `failureIdentity()` in
    `src/runStatus.ts` does: the platform's own status when the platform reported one
    (`android/133`), else the App's domain code (`observation/bleng.timeout`). Reading it out
    of the record is what makes the two witnesses comparable without restating the rule in
    Python; a detail that names neither is `None`, so the scene refuses it instead of
    accepting an unreadable failure.
    """
    platform = _RECORDED_PLATFORM.search(detail)
    if platform is not None:
        return f"{platform[1].casefold()}/{platform[2]}"
    domain = _RECORDED_DOMAIN.match(detail)
    return f"{domain[1]}/{domain[2]}" if domain is not None else None


class NativeSceneDriver:
    """One in-process driver phase at a time; nothing resident, nothing to retire.

    A subclass states the three facts this module cannot know: `kind`/`record_scope` (what the
    driver record calls itself), `_artifact()` (the exact bytes that ran, and its hash), and
    `_run_phase` (how one plan becomes one receipt). Everything else — validation, phase
    sequencing, host actions and the observation derivations — is shared.
    """

    # The evidence vocabulary's driver-kind token and the record's own scope.
    kind: str = ""
    record_scope: str = ""
    # Some platforms start a fresh App process per run (an instrumented test runs inside the
    # App's own process). Their scene is therefore one process with in-band host actions;
    # a platform that attaches to a resident process runs one plan per phase instead.
    one_process: bool = False

    def __init__(
        self,
        *,
        scene: SceneEvidence,
        device: str,
        app_id: str,
        output: Path,
        cwd: Path,
        fresh_install: bool = True,
        expected_local_name: str | None = None,
        service_filter: str | None = None,
        subscription: dict[str, str] | None = None,
        gatt: dict[str, str] | None = None,
    ) -> None:
        self._phases = phases_for(scene)
        if type(fresh_install) is not bool:
            raise ValueError("fresh_install must be an explicit bool")
        # Every planned scene starts by finding the declared peer, so an unnamed scan would
        # pass on any nearby radio and prove nothing about the DUT.
        if type(expected_local_name) is not str or not expected_local_name.strip():
            raise ValueError("the scene requires the fixture's expected local name")
        for name, value in (
            ("expected_local_name", expected_local_name),
            ("service_filter", service_filter),
        ):
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or absent")
        subscription_scene = scene.kind in SUBSCRIPTION_KINDS
        if subscription_scene:
            if type(subscription) is not dict or set(subscription) != {
                "uuid",
                "mode",
                "value",
            }:
                raise ValueError(
                    "a subscription scene needs the profile-derived uuid/mode/value"
                )
            if subscription["mode"] not in ("notification", "indication"):
                raise ValueError("unsupported subscription mode")
        elif subscription is not None:
            raise ValueError("this scene declares no subscription")
        required_gatt = GATT_TARGET_KEYS.get(scene.kind)
        if required_gatt is not None:
            if type(gatt) is not dict or set(gatt) != required_gatt:
                raise ValueError(
                    f"the {scene.kind} scene needs the profile-derived "
                    f"{sorted(required_gatt)} target"
                )
            if type(gatt["uuid"]) is not str or not gatt["uuid"].strip():
                raise ValueError("the GATT target needs a characteristic UUID")
            if "write" in required_gatt:
                text = gatt["write"]
                # The App's write input encodes UTF-8 text, so a payload that is not printable
                # ASCII could not be declared through this surface; refuse rather than write
                # something else. The length bound is the App's own input bound.
                if (
                    type(text) is not str
                    or not text
                    or len(text) > 16
                    or text.strip() != text
                    or not text.isprintable()
                    or not text.isascii()
                ):
                    raise ValueError(
                        "the io write payload must be 1..16 printable ASCII bytes"
                    )
        elif gatt is not None:
            raise ValueError("this scene declares no read/write target")
        self._scene = scene
        self._device = device
        self._app_id = app_id
        self._output = output.absolute()
        self._cwd = cwd.absolute()
        self._fresh_install = fresh_install
        self._expected_local_name = expected_local_name
        self._service_filter = service_filter
        self._subscription = subscription
        self._gatt = gatt
        self._hold: PeerHold | None = None
        self._run_id: str | None = None
        self._driven = False
        # Row -> phase, in the order `_merged_steps` emitted them. One-process scenes are
        # sliced back into phase views from this, so the plan and the slice can never
        # disagree about where a phase begins.
        self._merged_origin: list[dict[str, Any]] = []
        # The one run a one-process scene performed, unsliced: the run itself is what a
        # reviewer reads first, and every phase view is a derivation of it.
        self._run_document: dict[str, Any] | None = None

    # -- platform seams -------------------------------------------------------

    def _artifact(self) -> dict[str, Any]:
        """The exact artifact this lane runs, by kind and hash, for handoff and record."""
        raise NotImplementedError

    def _require_artifact(self) -> None:
        """Re-prove locally that the artifact has not changed (arms the driver)."""
        raise NotImplementedError

    def _require_platform(self, *, platform: str, candidate: Any, phone: Any) -> None:
        """Refuse a platform, candidate owner or phone lease this lane cannot drive."""
        raise NotImplementedError

    def _bind_candidate(
        self, candidate: Any, *, require_owner: Callable[[], None], deadline: float
    ) -> Callable[[], None]:
        """Take the App this run installed and launched, without a remote driver."""
        raise NotImplementedError

    def _run_phase(
        self, phase: Phase, raw: Path, options: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """Run one plan and return the driver's published result."""
        raise NotImplementedError

    def _run_scene_plan(
        self,
        raw: Path,
        options: dict[str, Any],
        *,
        timeout: float,
        on_handoff: Callable[[str], None],
    ) -> dict[str, Any]:
        """Run the whole scene as one plan, pausing for each host action in band."""
        raise NotImplementedError

    def _phase_extra(self, result: dict[str, Any]) -> dict[str, Any]:
        """Per-phase record fields this lane's executor can state; nothing by default."""
        return {}

    def _publish_diagnostics(
        self, raw: Path, phase: Phase, result: dict[str, Any]
    ) -> None:
        """Copy a phase's bounded textual diagnostics beside the receipts."""

    # -- identity -------------------------------------------------------------

    def _plans(self) -> dict[str, tuple[dict[str, Any], ...]]:
        return plans(
            fresh_install=self._fresh_install,
            attach=True,
            expected_local_name=self._expected_local_name,
            service_filter=self._service_filter,
            subscription=self._subscription,
            gatt=self._gatt,
            scene=self._scene.name,
            cycles=self._scene.cycles,
        )

    def _steps(self, phase: Phase) -> tuple[dict[str, Any], ...]:
        plans = self._plans()
        name = self._plan_name(phase)
        if name not in plans:
            raise RuntimeError(f"the driver lane has no plan {name!r}")
        return plans[name]

    @property
    def session(self) -> str:
        if self._run_id is None:
            raise RuntimeError("the native driver has no scene run yet")
        return f"{self.kind}:{self._run_id}"

    def require_handoff(
        self,
        *,
        platform: str,
        candidate: Any,
        phone: Any,
        run_id: str,
        app_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        self._require_platform(platform=platform, candidate=candidate, phone=phone)
        if app_id != self._app_id or phone.device != self._device:
            raise RuntimeError("native driver device/App identity differs from the run")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite absolute deadline required")
        if self._driven or self._run_id is not None:
            raise RuntimeError("the native driver is one-shot per scene")
        self._run_id = run_id
        return {
            "kind": self.kind,
            "session_id": self.session,
            **self._artifact(),
            "plan": self._scene.name,
            "phases": [phase.name for phase in self._phases],
            "steps": sum(len(self._steps(phase)) for phase in self._phases),
            "fresh_install": self._fresh_install,
            "subscription": None
            if self._subscription is None
            else {
                "uuid": self._subscription["uuid"],
                "mode": self._subscription["mode"],
            },
            "gatt": None
            if self._gatt is None
            else {
                "uuid": self._gatt["uuid"],
                "write_bytes": len(self._gatt["write"])
                if "write" in self._gatt
                else None,
            },
        }

    def arm(self, deadline: float) -> None:
        """Re-verify locally: the pinned artifact is unchanged and time remains.

        There is no remote session to re-prove and no HTTP budget to narrow. The App's own
        identity is the candidate's checkpoint at the scene boundaries.
        """
        self._require_artifact()
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite absolute deadline required")
        if deadline - time.monotonic() <= 0:
            raise TimeoutError("prepared scan deadline exceeded")

    # -- ownership ------------------------------------------------------------

    def bind(
        self,
        candidate: Any,
        *,
        require_owner: Callable[[], None],
        deadline: float,
    ) -> Callable[[], None]:
        return self._bind_candidate(
            candidate, require_owner=require_owner, deadline=deadline
        )

    def click_identity(self) -> Callable[[], None] | None:
        # An in-process target owns the taps it makes, so it cannot re-prove the process
        # between them. The identity checkpoint brackets the scene instead.
        return None

    # -- scene ----------------------------------------------------------------

    def drive(self, scene: SceneEvidence, options: dict[str, Any]) -> None:
        if scene is not self._scene:
            raise RuntimeError("native driver scene differs from the prepared scene")
        if self._driven:
            raise RuntimeError("the native driver is one-shot per scene")
        self._driven = True
        self._output.mkdir(mode=0o700, parents=True, exist_ok=False)
        timeout = options["timeout"]
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("the scene needs a finite positive budget")
        publish_new_json(
            self._output / "plan.json",
            {
                "schema_version": 1,
                "scene": scene.name,
                "attach": True,
                "fresh_install": self._fresh_install,
                "phases": [
                    {
                        "name": phase.name,
                        "host": phase.host,
                        "steps": list(self._steps(phase)),
                    }
                    for phase in self._phases
                ],
            },
        )
        # The raw runs (xcresult, xcodebuild log, staged xctestrun) are tool storage, not
        # evidence: they stay outside the bundle so a replay reads the derived witnesses
        # below rather than a multi-gigabyte result bundle.
        per_phase: dict[str, dict[str, Any]] = {}
        failure: BaseException | None = None
        observations: list[str] = []
        with tempfile.TemporaryDirectory(prefix="blehub-scene-") as temporary:
            raw = Path(temporary)
            try:
                if self.one_process:
                    # The whole scene is one process, with the host's actions announced and
                    # confirmed inside it. The single receipt is then sliced back into the
                    # phase views the trace derivations already expect, so a scene's meaning
                    # is identical on both platforms.
                    try:
                        merged = self._run_scene_plan(
                            raw,
                            options,
                            timeout=timeout,
                            on_handoff=lambda action: self._host_action(
                                action, options
                            ),
                        )
                        self._run_document = merged
                        per_phase = self._split_phases(merged)
                    except BaseException:
                        # The executor publishes its own result even when it fails, and that
                        # is what explains the failure in the bundle. Slicing can fail too,
                        # and the whole receipt is still the only thing that says why.
                        try:
                            retained = self._retained(
                                raw / self._scene.name / str(self._run_id)
                            )
                        except RuntimeError:
                            retained = None
                        if retained is not None:
                            keep = self._run_document or retained
                            self._run_document = keep
                            per_phase.setdefault(self._phases[0].name, keep)
                        raise
                else:
                    for phase in self._phases:
                        if phase.host is not None:
                            self._host_action(phase.host, options)
                        try:
                            per_phase[phase.name] = self._run_phase(
                                phase, raw, options, timeout=timeout
                            )
                        except BaseException:
                            # The lane publishes its own result even when the phase fails, and
                            # that result is what explains the failure in the bundle. Keeping
                            # it is best effort: a phase that never started has none, and the
                            # original failure must survive either way.
                            try:
                                per_phase[phase.name] = self._retained(
                                    raw / phase.name / str(self._run_id)
                                )
                            except RuntimeError:
                                pass
                            raise
                observations = self._observations(per_phase)
            except BaseException as error:  # noqa: BLE001 -- publish, then re-raise
                failure = error
            self._publish(raw, scene, per_phase, failure)
        if failure is not None:
            raise failure
        for observation in observations:
            options["observations"].append(observation)

    def _plan_name(self, phase: Phase) -> str:
        """The plan's identity: the scene alone when it has one phase, else scene.phase."""
        return (
            self._scene.name
            if len(self._phases) == 1
            else (f"{self._scene.name}.{phase.name}")
        )

    def _merged_steps(self) -> tuple[dict[str, Any], ...]:
        """Every phase's steps in order, with a `handoff` step where the host must act.

        In a one-process scene every read lands in one receipt, so a read name that appears in
        two phases would overwrite the earlier value. Each read is therefore emitted under
        `<phase>::<name>` and `_split_phases` renames it back, which keeps both the receipt and
        the per-phase views exactly what a per-phase run would have produced.
        """
        origin: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        for phase in self._phases:
            if phase.host is not None:
                steps.append(handoff_step(phase.host))
                origin.append({"phase": phase.name, "step": None})
            for step in self._steps(phase):
                emitted = dict(step)
                if self._prefix_reads and step.get("op") == "read":
                    name = step.get("as")
                    if type(name) is str and name:
                        emitted["as"] = f"{phase.name}::{name}"
                steps.append(emitted)
                origin.append({"phase": phase.name, "step": step})
        self._merged_origin = origin
        return tuple(steps)

    @property
    def _prefix_reads(self) -> bool:
        """Only a multi-phase one-process scene can collide on a read name."""
        return self.one_process and len(self._phases) > 1

    def _split_phases(self, merged: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Cut one process-wide receipt into the per-phase views the trace reads.

        Values are the reads each phase performed (restored to their plan-local names) and
        `steps` are that phase's own rows with the plan's original step objects, so a
        derivation sees exactly what it would have seen from a per-phase run.

        The slice boundaries come from `_merged_origin`, the same list the plan was built
        from, so a phase without a host action is simply a phase without a handoff row —
        not an arithmetic special case. A *failed* receipt is a prefix of its plan and is
        sliced as far as it goes; only a receipt that claims to have passed must be whole.
        """
        receipt = merged.get("receipt")
        if type(receipt) is not dict or type(receipt.get("steps")) is not list:
            raise RuntimeError("the merged scene receipt is incomplete")
        if not self._merged_origin:
            raise RuntimeError("the merged plan was never built for this driver")
        rows = receipt["steps"]
        values = receipt.get("values") or {}
        if len(rows) > len(self._merged_origin):
            raise RuntimeError("the merged scene receipt is longer than the plan")
        if len(rows) < len(self._merged_origin) and receipt.get("status") == "passed":
            raise RuntimeError("the merged scene receipt is shorter than the plan")
        grouped: dict[str, list[tuple[Any, dict[str, Any]]]] = {
            phase.name: [] for phase in self._phases
        }
        for index, origin in enumerate(self._merged_origin):
            step = origin.get("step")
            if step is None:  # a handoff row carries no plan step of its own
                continue
            row = rows[index] if index < len(rows) else None
            grouped[origin["phase"]].append((row, step))
        per_phase: dict[str, dict[str, Any]] = {}
        for phase in self._phases:
            steps = self._steps(phase)
            rows_for_phase = grouped[phase.name]
            reads: dict[str, Any] = {}
            sliced: list[dict[str, Any]] = []
            for row, step in rows_for_phase:
                if row is None:
                    break  # a failed receipt stops mid-plan; the rest never happened
                if type(row) is dict:
                    sliced.append({**row, "step": step})
                else:
                    sliced.append(row)
                if step.get("op") != "read":
                    continue
                name = step.get("as")
                key = f"{phase.name}::{name}" if self._prefix_reads else name
                if key in values:
                    reads[str(name)] = values[key]
            per_phase[phase.name] = {
                **merged,
                "receipt": {**receipt, "steps": sliced, "values": reads},
                # The plan this view is decoded against is the phase's own steps, so the
                # projection keys are the plan's own read names again.
                "projection": decode_projection(
                    {"steps": list(steps)},
                    {"values": reads},
                    # The whole receipt was already decoded strictly by the executor; this
                    # view is a derivation of it, so a read that phase never performed is
                    # simply absent here.
                    partial=True,
                ),
            }
        return per_phase

    @staticmethod
    def _retained(run_dir: Path) -> dict[str, Any]:
        path = run_dir / "result.json"
        if not path.is_file():
            raise RuntimeError("the scene driver published no result at all")
        return json.loads(path.read_text(encoding="utf-8"))

    def _host_action(self, action: str, options: dict[str, Any]) -> None:
        """Perform the host's own step between two runs of the App.

        The envelope's authority callback brackets it: the App, the phone lease and the
        fixture session must all still be this run's before the fixture is touched, and
        again before the App is driven once more.
        """
        if action == "publish":
            if self._subscription is None:
                raise RuntimeError("this scene declares no fixture emission")
            require_authority = options["require_app_authority"]
            fixture = options["fixture"]
            role = self._scene.characteristic_role
            if role is None:
                raise ValueError("the scene declares no characteristic role")
            require_authority()
            emitted = fixture.publish_notification(role).hex()
            require_authority()
            if emitted != self._subscription["value"]:
                raise RuntimeError(
                    "the fixture emitted a value the profile does not declare: "
                    f"{emitted} != {self._subscription['value']}"
                )
            return
        if action == "drop":
            # The peer's own end. Only the fixture can cause it, and the App is not told
            # anything: the scene reads what the App announced by itself afterwards.
            require_authority = options["require_app_authority"]
            fixture = options["fixture"]
            require_authority()
            peer = fixture.drop_peer()
            require_authority()
            if not peer:
                raise RuntimeError("the fixture dropped no peer")
            return
        if action == "withdraw":
            # The peer's undoing, proved on the controller itself: the central keeps the
            # advertisement it already saw, so the next attempt has an address to address and
            # nothing to answer it. The fixture refuses this unless its readback says the
            # advertising bit is clear, so the absence is a fixture fact, never an inference
            # from a failed connect.
            require_authority = options["require_app_authority"]
            fixture = options["fixture"]
            require_authority()
            fixture.withdraw_advertising()
            require_authority()
            return
        if action == "resume":
            # The peer offers itself again: the next cycle's scan has to find it, and the
            # scene states that instead of assuming a firmware that restarted advertising.
            #
            # `auto-connect` is the one scene whose claim is a standing "connect when the
            # peer appears" request, so a link that lands inside the resume's own readback is
            # its expected answer rather than a contradiction. Every other scene keeps the
            # strict refusal, because there the link must follow a scan it did not skip.
            require_authority = options["require_app_authority"]
            fixture = options["fixture"]
            require_authority()
            fixture.resume_advertising(
                expect_adoption=self._scene.kind == "auto-connect"
            )
            require_authority()
            return
        if action in {"hold-arm", "hold-verify", "hold-release", "hold-reject"}:
            # The peer's fault is armed, confirmed and resolved by the host; the App only
            # ever reports what it observed while the peer was holding the request.
            hold = self._peer_hold(options)
            if action == "hold-arm":
                hold.arm()
            elif action == "hold-verify":
                hold.require_held()
            elif action == "hold-release":
                hold.release()
            else:
                # A refusal is only possible while the request is held, so the confirmation
                # and the answer stay one guarded host action: `reject()` refuses unless the
                # firmware was proven to hold this very request.
                hold.require_held()
                hold.reject()
            return
        raise RuntimeError(f"unknown host action {action!r}")

    def _peer_hold(self, options: dict[str, Any]) -> PeerHold:
        """The one hold this scene may arm, re-guarded on every fixture command."""
        if self._hold is None:
            role = self._scene.characteristic_role
            if role is None:
                raise ValueError("the scene declares no characteristic role")
            operation, requested, before = _peer_hold_spec(
                self._scene.kind, self._gatt, self._subscription
            )
            self._hold = PeerHold(
                fixture=options["fixture"],
                role=role,
                operation=operation,
                requested=requested,
                before=before,
                guard=options["require_app_authority"],
            )
        return self._hold

    # -- trace ----------------------------------------------------------------

    def _observations(self, per_phase: dict[str, dict[str, Any]]) -> list[str]:
        if self._scene.kind == "scan":
            return self._scan_observations(per_phase)
        if self._scene.kind == "io":
            return self._io_observations(per_phase)
        if self._scene.kind == "cancellation":
            return self._cancellation_observations(per_phase)
        if self._scene.kind == "write-reject":
            return self._write_reject_observations(per_phase)
        if self._scene.kind in REPEATING_SCENES:
            return self._recovery_observations(per_phase)
        if self._scene.kind == "passive-disconnect":
            return self._passive_disconnect_observations(per_phase)
        if self._scene.kind == "ccc-enable-reject":
            return self._ccc_enable_observations(per_phase)
        if self._scene.kind == "ccc-disable-reject":
            return self._ccc_disable_observations(per_phase)
        if self._scene.kind == "background":
            return self._background_observations(per_phase)
        if self._scene.kind == "subscription":
            return self._subscription_observations(per_phase)
        if self._scene.kind == "disconnect":
            return self._disconnect_observations(per_phase)
        if self._scene.kind == "connect-failure":
            return self._connect_failure_observations(per_phase)
        if self._scene.kind == "link-facts":
            return self._link_facts_observations(per_phase)
        if self._scene.kind == "descriptor-io":
            return self._descriptor_io_observations(per_phase)
        if self._scene.kind == "devices-query":
            return self._devices_query_observations(per_phase)
        if self._scene.kind == "devices-kinds":
            return self._devices_kinds_observations(per_phase)
        if self._scene.kind == "adapter-control":
            return self._adapter_control_observations(per_phase)
        if self._scene.kind == "auto-connect":
            return self._auto_connect_observations(per_phase)
        raise RuntimeError(
            f"no observation derivation for a {self._scene.kind!r} scene"
        )

    def _scan_observations(self, per_phase: dict[str, dict[str, Any]]) -> list[str]:
        result = per_phase[self._phases[0].name]
        projection = result["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
            self._fact(projection, "scanning", "scan", "running", "scan:running"),
        ]
        self._assert_fact(projection, "scanning", "failure", "none")
        matched = self._advertisement(result)
        observed.append(matched)
        observed.append(
            self._fact(projection, "stopped", "scan", "stopped", "scan:stopped")
        )
        observed.append(self._fact(projection, "closed", "state", "closed", "closed"))
        self._require_ledger(result, SCAN_LEDGER)
        return self._require_trace(observed)

    def _background_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """The link and its subscription outlive a background/foreground cycle.

        The host emits while the App is away, so the second value can only arrive after the
        App returns — a value that arrived without it would describe a link that never left.
        Every assertion is the App's own report; the fixture's publish is what makes a silent
        loss visible instead of indistinguishable from a scene that never asked.
        """
        opened = per_phase["open"]
        value = per_phase["value"]
        away = per_phase["away"]
        back = per_phase["back"]
        assert self._subscription is not None
        mode = self._subscription["mode"]
        first = opened["projection"]
        observed = [
            self._fact(first, "initial", "state", "idle", "idle"),
            self._fact(first, "started", "state", "started", "started"),
            self._fact(first, "scanning", "scan", "running", "scan:running"),
        ]
        self._assert_fact(first, "scanning", "failure", "none")
        observed.append(self._advertisement(opened))
        observed.append(self._fact(first, "stopped", "scan", "stopped", "scan:stopped"))
        observed.append(
            self._fact(first, "linked", "link", "connected", "link:connected")
        )
        observed.append(self._fact(first, "topology", "gatt", "ready", "gatt:ready"))
        subscribed = per_phase["subscribe"]["projection"]
        observed.append(
            self._fact(
                subscribed, "subscribed", "notify", "subscribed", "notify:subscribed"
            )
        )
        self._assert_fact(subscribed, "subscribed", "notifyMode", mode)
        if self._count(value["projection"], "notified", "notifyTotal") < 1:
            raise RuntimeError(
                "the App reported no notification value before the cycle"
            )
        observed.append("notify:value-matched")
        # The `away` phase exists to put the App in the background and to fail if it is not
        # there, so a passing phase is exactly the fact this observation states — but only if
        # the receipt shows the window was actually held. A driver that quietly stopped
        # holding it would otherwise pass this scene on the strength of its status alone.
        self._require_dwell(away)
        observed.append("notify:backgrounded")
        resumed = back["projection"]
        observed.append(
            self._fact(resumed, "resumed", "link", "connected", "notify:resumed")
        )
        self._assert_fact(resumed, "resumed", "notify", "subscribed")
        # Two values total: the one before the cycle and the one the host emitted while the
        # App was away. Fewer means the background window lost it.
        if self._count(resumed, "drained", "notifyTotal") < 2:
            raise RuntimeError(
                "the value emitted while the App was away never arrived; the subscription "
                "did not outlive the background window"
            )
        observed.append("notify:value-after-resume")
        observed.append(self._fact(resumed, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    @staticmethod
    def _require_dwell(result: dict[str, Any]) -> None:
        """The background window is a temporal claim, so the receipt must have sampled it.

        Same rule as the quiet window in `passive-disconnect`: the plan states the window and
        the driver publishes how long the App was actually away, so the host re-checks the
        evidence instead of trusting a step's status. A short sample is a refusal, not a
        rounding difference: only the last decimal place of the driver's own report is
        forgiven.
        """
        if result.get("status") != "passed":
            raise RuntimeError("the background phase did not pass")
        for step in (result.get("receipt") or {}).get("steps") or []:
            planned = step.get("step") or {}
            if planned.get("op") != "background":
                continue
            observed = str((step.get("observed") or {}).get("background", ""))
            match = re.fullmatch(r"not-foreground for=(\d+(?:\.\d+)?)s.*", observed)
            window = planned.get("for")
            if match is None:
                raise RuntimeError(
                    f"the background window was not sampled: {observed!r}"
                )
            if (
                type(window) not in (int, float)
                or float(match.group(1)) + 0.05 < window
            ):
                raise RuntimeError(
                    "the App was away "
                    f"{match.group(1)}s of the declared {window}s background window"
                )
            return
        raise RuntimeError("the plan carried no background window")

    def _subscription_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        subscribe = per_phase["subscribe"]
        collect = per_phase["collect"]
        first = subscribe["projection"]
        last = collect["projection"]
        assert self._subscription is not None
        observed = [
            self._fact(first, "initial", "state", "idle", "idle"),
            self._fact(first, "started", "state", "started", "started"),
            self._fact(first, "scanning", "scan", "running", "scan:running"),
            self._advertisement(subscribe),
            self._fact(first, "stopped", "scan", "stopped", "scan:stopped"),
            self._fact(first, "linked", "link", "connected", "link:connected"),
            self._fact(first, "topology", "gatt", "ready", "gatt:ready"),
        ]
        self._assert_fact(first, "topology", "gattMatch", "one")
        observed.append(
            self._fact(first, "subscribed", "notify", "subscribed", "notify:subscribed")
        )
        self._assert_fact(first, "subscribed", "notifyMode", self._subscription["mode"])
        if self._count(last, "notified", "notifyTotal") < 1:
            raise RuntimeError("the App reported no notification value")
        observed.append("notify:value-matched")
        actions = self._require_ledger(collect, SUBSCRIPTION_LEDGER)
        if "notify-disable" not in actions:
            raise RuntimeError("the App recorded no accepted disable")
        observed.append("notify:disabled")
        observed.append(self._fact(last, "ended", "notify", "ended", "notify:ended"))
        observed.append(self._fact(last, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _disconnect_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """An end this App submitted, and the lane it took with it.

        The scene has to prove the link and its subscription were real before the end can mean
        anything, so a read and an emitted value come first. What it then asserts cannot be
        read off any single screen: the end names the operator, the subscription is retired
        rather than left claiming `subscribed` on a dead link, no further value is counted
        across the end, and all four facts still hold after the quiet window. The App's own
        ledger is the second witness and is what distinguishes this scene from a peer's end.
        """
        opened = per_phase["open"]
        value = per_phase["value"]
        severed = per_phase["sever"]
        subscription = self._subscription
        gatt = self._gatt
        if subscription is None or gatt is None:
            raise RuntimeError("the disconnect scene needs its declared targets")
        characteristic = gatt["uuid"]
        observed, properties = self._live_facts(opened)
        if "read" not in properties:
            raise RuntimeError(
                f"the declared {characteristic} is not readable: {properties}"
            )
        self._value_read(opened, "disconnect-initial", characteristic)
        observed.append("read:initial")
        self._assert_fact(opened["projection"], "subscribed", "notify", "subscribed")
        self._assert_fact(
            opened["projection"], "subscribed", "notifyMode", subscription["mode"]
        )
        observed.append("notify:subscribed")

        delivered = self._count(value["projection"], "notified", "notifyTotal")
        if delivered < 1:
            raise RuntimeError("the App reported no notification value")
        observed.append("notify:value-matched")

        projection = severed["projection"]
        self._assert_fact(projection, "severed", "link", "disconnected")
        self._assert_fact(projection, "severed", "linkEnd", "requested")
        observed.append("link:end-requested")
        # A subscription cannot outlive the link that carries it. A phase still claiming
        # `subscribed` here is the App promising delivery over a link it just reported dead.
        observed.append(
            self._fact(projection, "severed", "notify", "ended", "notify:ended")
        )
        after = self._count(projection, "severed", "notifyTotal")
        if after != delivered:
            raise RuntimeError(
                f"the App counted a value across its own end: {delivered} -> {after}"
            )

        silent = severed["projection"]
        self._assert_fact(silent, "silent", "link", "disconnected")
        self._assert_fact(silent, "silent", "linkEnd", "requested")
        self._assert_fact(silent, "silent", "notify", "ended")
        if self._count(silent, "silent", "notifyTotal") != delivered:
            raise RuntimeError("the App counted a value during the quiet window")
        self._require_quiet_step(severed)
        observed.append("link:silent")

        self._require_withdrawn_controls(severed)
        observed.append("controls:withdrawn")

        self._require_ledger(severed, DISCONNECT_LEDGER)
        observed.append(self._fact(silent, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _descriptor_io_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """The peer's own CCCD, correlated with the App's subscription in both directions.

        A subscription claim has two witnesses: the App's `notify` phase and the CCCD the peer
        stores. This scene makes them agree at every turn — disabled before, enabled after the
        enable, unchanged by a same-value write, and cleared by the disable — so an App that
        claims a subscription the peer never enabled cannot pass, and neither can a peer that
        keeps delivering after a disable the App reported.
        """
        result = per_phase[self._phases[0].name]
        subscription = self._subscription
        gatt = self._gatt
        if subscription is None or gatt is None:
            raise RuntimeError("the descriptor scene needs its declared targets")
        observed, properties = self._live_facts(result)
        if "read" not in properties:
            raise RuntimeError(
                f"the declared {gatt['uuid']} is not readable: {properties}"
            )
        mode = subscription["mode"]
        if mode not in CCC_BITS:
            raise ValueError(f"unsupported notification mode {mode!r}")
        enabled = CCC_BITS[mode].hex()
        projection = result["projection"]
        observed.append(
            self._descriptor_fact(result, "cccd-initial", "0000", "cccd:initial")
        )
        observed.append(
            self._fact(
                projection, "subscribed", "notify", "subscribed", "notify:subscribed"
            )
        )
        self._assert_fact(projection, "subscribed", "notifyMode", mode)
        observed.append(
            self._descriptor_fact(result, "cccd-enabled", enabled, "cccd:enabled")
        )
        # The peer first, then the direct write: a disabled CCCD read *before* the write means
        # the write cannot explain a stale value.
        observed.append(
            self._fact(projection, "ended", "notify", "ended", "notify:disabled")
        )
        observed.append(
            self._descriptor_fact(result, "cccd-cleared", "0000", "cccd:cleared")
        )
        # One trace for both platforms: the marker says the write *question* was answered.
        # Which answer it got is pinned here, so neither platform can pass by answering the
        # other's way.
        answered = self._value(projection, "cccd-written", "descriptorWrite")
        if answered == "unsupported":
            # CoreBluetooth forbids a direct CCCD write and demands `setNotifyValue(_:for:)`, so
            # the platform's own refusal is the answer — and it must be its typed code, not a
            # bare failure. The write never happened, so the value must still be the one the
            # disable left behind; the read below proves exactly that.
            failure = self._value(projection, "cccd-written", "failure")
            if not is_not_supported(failure):
                raise RuntimeError(
                    "the descriptor write says unsupported but its failure is "
                    f"{failure!r}; only the platform's own refusal may end it that way"
                )
        elif answered != "done":
            raise RuntimeError(
                f"the peer did not accept the same-value descriptor write: {answered!r}"
            )
        observed.append("cccd:write-answered")
        observed.append(
            self._descriptor_fact(result, "cccd-unchanged", "0000", "cccd:unchanged")
        )
        self._require_descriptor_io_ledger(result)
        observed.append(self._fact(projection, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _devices_query_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """The platform's own `connected` inventory, checked against the live link.

        A list proves the command was accepted; only the peer key proves the inventory
        describes *this* run's link. `devicesPeer=no` is a real answer — an inventory that
        omits the peer the App is holding is exactly the divergence this scene exists to
        catch — so the scene refuses it instead of accepting "a list came back".
        """
        result = per_phase[self._phases[0].name]
        projection = result["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
            self._fact(projection, "scanning", "scan", "running", "scan:running"),
            self._advertisement(result),
            self._fact(projection, "stopped", "scan", "stopped", "scan:stopped"),
            self._fact(projection, "linked", "link", "connected", "link:connected"),
        ]
        self._assert_fact(projection, "linked", "failure", "none")
        phase = self._value(projection, "devices", "devices")
        if phase != "done":
            raise RuntimeError(f"the connected inventory did not settle: {phase!r}")
        self._assert_fact(projection, "devices", "failure", "none")
        count = self._value(projection, "devices", "devicesCount")
        if not count.isdigit() or int(count) < 1:
            raise RuntimeError(
                f"the connected inventory returned no usable entries: {count!r}"
            )
        observed.append("devices:listed")
        peer = self._value(projection, "devices", "devicesPeer")
        if peer != "yes":
            raise RuntimeError(
                "the connected inventory does not contain the peer this run is linked to: "
                f"{peer!r}"
            )
        observed.append("devices:peer-connected")
        self._require_ledger(result, DEVICES_QUERY_LEDGER)
        observed.append(self._fact(projection, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _auto_connect_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A standing connect request, answered by the platform rather than by optimism.

        The peer is withdrawn before the request and restored after it, so a link can only
        appear because the platform was waiting for a peer that was not there. Android answers
        by linking; Apple has no standing-connect capability and refuses with its own
        `not_supported` code. Both are answers. A timeout, a bare `failed`, or a link that
        appears while the peer is still withdrawn are not.
        """
        opened = per_phase["open"]
        settled = per_phase["settle"]
        projection = opened["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
            self._fact(projection, "scanning", "scan", "running", "scan:running"),
            self._advertisement(opened),
            self._fact(projection, "stopped", "scan", "stopped", "scan:stopped"),
            # The host's own act: the fixture withdrew advertising and proved it on the
            # controller. The App was told nothing, so this fact is not the App's to claim.
            "peer:withdrawn",
        ]
        entries = self._ledger_entries(settled)
        self._require_auto_connect_ledger(entries)
        answer = self._value(settled["projection"], "auto", "auto")
        if answer == "done":
            # The strong claim: the peer was gone when the request was made, so this link is
            # the standing request's effect and not the plain connect path.
            self._assert_fact(settled["projection"], "auto", "link", "connected")
            self._assert_fact(settled["projection"], "auto", "failure", "none")
        elif answer == "unsupported":
            failure = self._value(settled["projection"], "auto", "failure")
            if not is_not_supported(failure):
                raise RuntimeError(
                    f"the auto-connect lane says unsupported but its failure is {failure!r}; "
                    "only the platform's own refusal may end it as unsupported"
                )
            self._assert_fact(settled["projection"], "auto", "link", "none")
        else:
            raise RuntimeError(
                "the standing connect request was never answered: "
                f"auto={answer!r} (a timeout is not an answer)"
            )
        observed.append("auto:answered")
        observed.append(
            self._fact(settled["projection"], "closed", "state", "closed", "closed")
        )
        return self._require_trace(observed)

    def _devices_kinds_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """The inventories the main line does not need, as each platform answers them.

        Neither kind needs a peer or a link, so this scene proves only one thing per kind:
        the answer is the platform's own. Android reads `bonded` and `companion` inventories;
        Apple has neither and refuses with its own `not_supported` code. A timeout, a bare
        `failed`, or an answer that names a different kind than the one asked for is not.
        """
        result = per_phase[self._phases[0].name]
        projection = result["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
        ]
        for kind in ("bonded", "companion"):
            if self._value(projection, kind, "devicesKind") != kind:
                raise RuntimeError(
                    f"the {kind} reading answers a different inventory: "
                    f"{self._value(projection, kind, 'devicesKind')!r}"
                )
            answer = self._value(projection, kind, "devices")
            if answer == "done":
                self._assert_fact(projection, kind, "failure", "none")
                count = self._value(projection, kind, "devicesCount")
                if not count.isdigit():
                    raise RuntimeError(
                        f"the {kind} inventory reported no usable count: {count!r}"
                    )
            elif answer == "unsupported":
                failure = self._value(projection, kind, "failure")
                if not is_not_supported(failure):
                    raise RuntimeError(
                        f"the {kind} inventory says unsupported but its failure is "
                        f"{failure!r}; only the platform's own refusal may end it that way"
                    )
            else:
                raise RuntimeError(
                    f"the {kind} inventory was never answered: {answer!r} "
                    "(a timeout is not an answer)"
                )
            observed.append(f"devices:{kind}-answered")
        self._require_paired_submission_ledger(result, "devices-query")
        observed.append(self._fact(projection, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _adapter_control_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Radio power, both directions, as each platform answers it.

        The App may never claim a change the platform did not report: `done` means the platform
        accepted the request, `unsupported` means it refused and that refusal must name its own
        `not_supported`. Both directions must answer the direction they asked for, and asking
        must leave the observation main line alone — a radio request is not a scan or a link,
        and an App that quietly stopped scanning would show up here.
        """
        result = per_phase[self._phases[0].name]
        projection = result["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
        ]
        for direction in ("off", "on"):
            if self._value(projection, direction, "radioTarget") != direction:
                raise RuntimeError(
                    f"the {direction} request answered a different direction: "
                    f"{self._value(projection, direction, 'radioTarget')!r}"
                )
            answer = self._value(projection, direction, "radio")
            if answer == "done":
                self._assert_fact(projection, direction, "failure", "none")
            elif answer == "unsupported":
                failure = self._value(projection, direction, "failure")
                if not is_not_supported(failure):
                    raise RuntimeError(
                        f"the {direction} request says unsupported but its failure is "
                        f"{failure!r}; only the platform's own refusal may end it that way"
                    )
            else:
                raise RuntimeError(
                    f"the {direction} request was never answered: {answer!r} "
                    "(a timeout is not an answer)"
                )
            # Radio power is an adapter fact: asking must not touch the main line.
            self._assert_fact(projection, direction, "scan", "idle")
            self._assert_fact(projection, direction, "link", "none")
            observed.append(f"radio:{direction}-answered")
        self._require_paired_submission_ledger(result, "adapter-control")
        observed.append(self._fact(projection, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _require_paired_submission_ledger(
        self, result: dict[str, Any], action: str
    ) -> None:
        """Two submissions of one owner command, each with its own wait unless refused.

        Two scenes share this shape — the two system inventories and the two radio directions —
        so the rule lives once. The shape is derived from the entries rather than pinned as one
        constant: a platform may answer one and refuse the other (an older Android without
        companion associations, or a device that really can change the radio), and any refusal
        here must be the platform's own `not_supported`, never a wait that failed for another
        reason.
        """
        entries = self._ledger_entries(result)
        names = _collapse([(entry[1], entry[2]) for entry in entries])
        submissions = [entry for entry in entries if entry[1] == action]
        if len(submissions) != 2:
            raise RuntimeError(
                "the scene submits that command twice, so its record must hold two "
                f"submissions: {names}"
            )
        expected: list[tuple[str, str]] = [("start", "")]
        for _, _, outcome, detail in submissions:
            expected.append((action, outcome))
            if outcome == "succeeded":
                expected.append((f"{action}-wait", outcome))
            elif not is_not_supported(detail):
                raise RuntimeError(
                    "the owner-command record failed for a reason other than a missing "
                    f"capability: {entries}"
                )
        expected.append(("close", ""))
        if names != _collapse(expected):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{names} != {_collapse(expected)}"
            )

    def _require_auto_connect_ledger(
        self, entries: list[tuple[int, str, str, str]]
    ) -> None:
        """The App's own record: the standing request, plus its wait when the platform has one.

        A platform without the capability says so with its own `not_supported`, and that answer
        can land at either boundary: a submission the engine rejects before queueing (no wait
        row exists) or a terminal the backend reports for the queued operation (both rows do).
        CoreBluetooth takes the second shape — `beginAutoConnect` is accepted and the operation
        fails with the platform's reason — so accepting only the first made a correct platform
        answer look like a broken record. Everything else must be succeeded and must be exactly
        this scene.
        """
        failed = [
            (action, detail)
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
        ]
        refusal_boundary: str | None = None
        if failed:
            if len(failed) == 1 and is_not_supported(failed[0][1]):
                refusal_boundary = failed[0][0]
            if refusal_boundary not in ("auto-connect", "auto-connect-wait"):
                raise RuntimeError(
                    "the App's standing-request record failed for a reason other than a "
                    f"missing capability: {failed}"
                )
        expected = tuple(
            name
            for name in AUTO_CONNECT_LEDGER
            if not (refusal_boundary == "auto-connect" and name == "auto-connect-wait")
        )
        names = _collapse([(action, outcome) for _, action, outcome, _ in entries])
        if names != list(expected):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{names} != {list(expected)}"
            )

    def _descriptor_fact(
        self, result: dict[str, Any], read: str, expected: str, observation: str
    ) -> str:
        """One descriptor reading that must be the peer's own bytes.

        A missing terminal is refused separately from a wrong value: "the read never settled"
        and "the peer said something else" are different failures, and a scene that merged
        them would report the wrong cause.
        """
        projection = result["projection"]
        terminal = self._value(projection, read, "descriptor")
        if terminal != "done":
            raise RuntimeError(
                f"the {read} reading has no descriptor value; its terminal is {terminal!r}"
            )
        self._assert_fact(projection, read, "descriptorValue", expected)
        return observation

    def _live_facts(self, opened: dict[str, Any]) -> tuple[list[str], list[str]]:
        """The facts every managed scene observes before it acts, plus the target's properties.

        A scene has to reach the peer the same way each time: one manual scan that matched the
        declared fixture, one link, one discovery, one target resolved by identity. Returning
        the capabilities lets each scene require the property its own command needs instead of
        assuming the topology.
        """
        projection = opened["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
            self._fact(projection, "scanning", "scan", "running", "scan:running"),
            self._advertisement(opened),
            self._fact(projection, "stopped", "scan", "stopped", "scan:stopped"),
            self._fact(projection, "linked", "link", "connected", "link:connected"),
            self._fact(projection, "topology", "gatt", "ready", "gatt:ready"),
        ]
        self._assert_fact(projection, "topology", "gattMatch", "one")
        return observed, self._value(projection, "topology", "gattProps").split(",")

    def _io_observations(self, per_phase: dict[str, dict[str, Any]]) -> list[str]:
        """Read, write, read back and RSSI, from the App's own published outcomes.

        The scene never claims what the peer's pre-write value is: a profile's
        `initial_value_hex` describes the image as written, and what the installed peer
        currently holds can only be observed. What it does claim is that a value came back,
        that the write settled as a receipt naming this characteristic with this length,
        and that a read after that receipt reports something else.
        """
        result = per_phase[self._phases[0].name]
        assert self._gatt is not None
        characteristic = self._gatt["uuid"]
        write_text = self._gatt["write"]
        # The peer's own declared capabilities decide what the scene may attempt, exactly
        # as they decide the write type the App uses.
        observed, properties = self._live_facts(result)
        if "read" not in properties:
            raise RuntimeError(
                f"the declared {characteristic} is not readable: {properties}"
            )
        if not {"write", "writeNoResponse"} & set(properties):
            raise RuntimeError(
                f"the declared {characteristic} is not writable: {properties}"
            )
        initial = self._value_read(result, "io-initial", characteristic)
        observed.append("io:read-initial")
        write_type = "withResponse" if "write" in properties else "withoutResponse"
        receipt = f"{characteristic}: wrote {len(write_text)} bytes ({write_type})"
        written = self._reading(result, "io-written")
        if written.casefold() != receipt.casefold():
            raise RuntimeError(
                f"the write receipt names something else: {written!r} != {receipt!r}"
            )
        observed.append("io:written")
        readback = self._value_read(result, "io-readback", characteristic)
        if readback == initial:
            raise RuntimeError(
                "the peer still reports its pre-write value after a write; "
                f"a write receipt is not a stored value ({readback!r})"
            )
        observed.append("io:readback")
        rssi = self._reading(result, "io-rssi")
        reading = re.fullmatch(r"(-?[0-9]{1,4}) dBm", rssi)
        if reading is None:
            raise RuntimeError(f"the RSSI outcome is not a reading: {rssi!r}")
        if int(reading.group(1)) >= 0:
            raise RuntimeError(f"a connected RSSI must be negative: {rssi!r}")
        observed.append("rssi:observed")
        self._require_ledger(result, IO_LEDGER)
        observed.append(
            self._fact(result["projection"], "closed", "state", "closed", "closed")
        )
        return self._require_trace(observed)

    def _cancellation_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A queued read cancelled behind a peer-held one, then the lane recovering.

        The peer is what makes this deterministic: the host proves the firmware is holding
        the first read before the second is submitted, so the second is genuinely *queued*.
        Every claim below is the App's own publication — a cancel request is not a cancel
        result, and the hold itself proves nothing about the App.
        """
        opened = per_phase["open"]
        hold = per_phase["hold"]
        queue = per_phase["queue"]
        settle = per_phase["settle"]
        assert self._gatt is not None
        characteristic = self._gatt["uuid"]
        observed, properties = self._live_facts(opened)
        if "read" not in properties:
            raise RuntimeError(
                f"the declared {characteristic} is not readable: {properties}"
            )
        held = hold["projection"]
        self._assert_fact(held, "held", "gattRead", "waiting")
        self._assert_fact(held, "held", "gattReads", "1")
        observed.append("read:held")

        queued = queue["projection"]
        self._assert_fact(queued, "queued", "gattReads", "2")
        observed.append("read:queued")
        self._assert_fact(queued, "cancelled", "gattRead", "cancelled")
        # The held read is still outstanding: the cancel retired the queued one only.
        self._assert_fact(queued, "cancelled", "gattReads", "1")
        if not self._reading(queue, "cancel").strip():
            raise RuntimeError("the cancelled read published no cancelled terminal")
        observed.append("cancel:queued-accepted")

        last = settle["projection"]
        self._assert_fact(last, "completed", "gattRead", "done")
        self._assert_fact(last, "completed", "gattReads", "0")
        self._value_read(settle, "completed-value", characteristic)
        observed.append("read:active-completed")
        # The recovery read is its own settled read: the count is what says so, because the
        # App still shows the completed read's rendering until the new one overwrites it.
        self._assert_fact(last, "recovered", "gattReadTotal", "3")
        self._assert_fact(last, "recovered", "gattRead", "done")
        self._value_read(settle, "recovered-value", characteristic)
        observed.append("read:recovered")

        # The ledger is cumulative for the whole run, so it is checked once, at the end;
        # it is also the only record that names the cancelled wait as the failure it was.
        self._require_cancellation_ledger(settle)
        observed.append(self._fact(last, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _write_reject_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A write the peer refuses inside the firmware, then the same write succeeding.

        A refusal proves nothing by itself, so the stored value is read again and must
        equal what the peer reported before the write; only then is the same request
        repeated without a hold, and its read-back must be exactly what was written. The
        refusal is the *peer's* answer: the App's failed wait has to name the ATT status
        the firmware contract declares, not a local timeout.
        """
        opened = per_phase["open"]
        dispatch = per_phase["dispatch"]
        settle = per_phase["settle"]
        assert self._gatt is not None
        characteristic = self._gatt["uuid"]
        payload = self._gatt["write"].encode("ascii")
        observed, properties = self._live_facts(opened)
        if "read" not in properties:
            raise RuntimeError(
                f"the declared {characteristic} is not readable: {properties}"
            )
        if not {"write", "writeNoResponse"} & set(properties):
            raise RuntimeError(
                f"the declared {characteristic} is not writable: {properties}"
            )
        initial = self._value_read(opened, "initial-value", characteristic)
        # The payload is the host's, derived from the profile; a payload the peer already
        # holds would make "the value did not change" prove nothing.
        if initial == render_gatt_value(payload):
            raise RuntimeError(
                "the declared write payload is already the value the peer holds; "
                "a read-back could not tell a commit from a no-op"
            )
        observed.append("read:initial")

        # Nothing is published while a write waits, so the App's own record is the fact
        # that the submission was accepted and its wait is outstanding.
        entries = self._ledger_entries(dispatch)
        accepted = [(action, outcome) for _, action, outcome, _ in entries][-2:]
        if accepted != [("gatt-write", "succeeded"), ("gatt-write-wait", "pending")]:
            raise RuntimeError(
                "the App did not record an accepted write whose wait is outstanding: "
                f"{accepted}"
            )
        observed.append("write:held")

        rejection = self._reading(settle, "rejection")
        if att_status_token(rejection) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the refusal does not name the peer's ATT status "
                f"{REJECT_ATT_RESULT}: {rejection!r}"
            )
        identity = self._value(settle["projection"], "rejected", "failure")
        if att_status_token(identity) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the projection's failure identity is not the peer's refusal: "
                f"{identity!r}"
            )
        observed.append("write:rejected")

        unchanged = self._value_read(settle, "unchanged", characteristic)
        if unchanged != initial:
            raise RuntimeError(
                "the peer changed its value after refusing the write: "
                f"{initial!r} -> {unchanged!r}"
            )
        observed.append("value:unchanged")

        write_type = "withResponse" if "write" in properties else "withoutResponse"
        receipt = f"{characteristic}: wrote {len(payload)} bytes ({write_type})"
        recovered = self._reading(settle, "recovered")
        if recovered.casefold() != receipt.casefold():
            raise RuntimeError(
                f"the recovered write's receipt is not this scene's: {recovered!r} "
                f"!= {receipt!r}"
            )
        observed.append("write:recovered")

        stored = self._value_read(settle, "stored", characteristic)
        if stored != render_gatt_value(payload):
            raise RuntimeError(
                f"the peer reports {stored!r} after a successful write of "
                f"{render_gatt_value(payload)!r}"
            )
        observed.append("value:stored")

        self._require_write_reject_ledger(settle)
        observed.append(
            self._fact(settle["projection"], "closed", "state", "closed", "closed")
        )
        return self._require_trace(observed)

    def _recovery_observations(self, per_phase: dict[str, dict[str, Any]]) -> list[str]:
        """N peer-initiated ends on one owner: every cycle repeats the whole preamble.

        Only the first cycle opens the session; each later one re-enters the same App, so a
        stale session, a resurrected capability or an owner that quietly needed replacing
        shows up as a cycle that cannot reach its own read. The read is attributed by the
        settled count, because the previous cycle's rendering is still on screen.
        """
        cycles = self._scene.cycles
        if type(cycles) is not int:
            raise ValueError("a repeating scene must declare its cycle count")
        first = per_phase[self._phases[0].name]
        assert self._gatt is not None
        characteristic = self._gatt["uuid"]
        # The scene's vocabulary is per cycle, so only the session's own two facts are
        # generic here; each cycle's preamble is checked in the cycle that produced it.
        projection = first["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
        ]
        for index in range(cycles):
            scan = per_phase[f"scan:{index}"]
            frame = scan["projection"]
            self._assert_fact(frame, "scanning", "scan", "running")
            self._assert_fact(frame, "stopped", "scan", "stopped")
            # The advertisement this cycle matched has to name the declared peer: the pull
            # loop accepts any rendering, and a neighbour would make the cycle meaningless.
            self._advertisement(scan)
            observed.append(f"cycle:{index}:scan")

            self._assert_fact(frame, "linked", "link", "connected")
            self._assert_fact(frame, "topology", "gatt", "ready")
            self._assert_fact(frame, "topology", "gattMatch", "one")
            properties = self._value(frame, "topology", "gattProps").split(",")
            if "read" not in properties:
                raise RuntimeError(
                    f"the declared {characteristic} is not readable: {properties}"
                )
            # This cycle's own read: a scan is a run in the App, so its read lane starts at
            # zero and the value on screen is this cycle's first settled read.
            served = scan["projection"]
            self._assert_fact(served, f"cycle-{index}-served", "gattReadTotal", "1")
            self._assert_fact(served, f"cycle-{index}-served", "gattRead", "done")
            self._value_read(scan, f"cycle-{index}-value", characteristic)
            observed.append(f"cycle:{index}:link")

            # The host's own action: the driver record states it, and the App was told
            # nothing.
            observed.append(f"cycle:{index}:dropped")
            announced = per_phase[f"announce:{index}"]["projection"]
            self._assert_fact(
                announced, f"cycle-{index}-announced", "link", "disconnected"
            )
            self._assert_fact(
                announced, f"cycle-{index}-announced", "linkEnd", "unrequested"
            )
            observed.append(f"cycle:{index}:announced")

        # The end lane announces every end: nothing else may have learned it. The App's
        # record is one bounded window over the whole session (`omitted=<n>` says how much
        # it dropped), so the claim it can carry is that nothing visible failed and that the
        # last cycle's own record is complete — the per-cycle read count is asserted above,
        # from the projection of the run that read.
        entries = self._ledger_entries(per_phase[f"announce:{cycles - 1}"])
        failed = [action for _, action, outcome, _ in entries if outcome != "succeeded"]
        if failed:
            raise RuntimeError(
                f"the App recorded a failed action during a recovery: {failed}"
            )
        names = _collapse([(action, outcome) for _, action, outcome, _ in entries])
        starts = [index for index, name in enumerate(names) if name == "begin-scan"]
        if not starts:
            raise RuntimeError("the App's record carries no scan in its visible window")
        tail = tuple(names[starts[-1] :])
        if tail != _RECOVERY_CYCLE_LEDGER:
            raise RuntimeError(
                "the last cycle's record is not a complete cycle: "
                f"{tail} != {list(_RECOVERY_CYCLE_LEDGER)}"
            )
        observed.append(
            self._fact(
                per_phase[f"announce:{cycles - 1}"]["projection"],
                "closed",
                "state",
                "closed",
                "closed",
            )
        )
        return self._require_trace(observed)

    def _passive_disconnect_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A peer-initiated end: the App must announce it by itself and then stay quiet.

        Nothing on screen may be taken from a command that caused the end, so the plan taps
        nothing before the announced end is read. Three separate facts follow from that: the
        announced reason cannot blame the operator, the quiet window holds for the stated
        time (value and identity included), and every control that needs a link is withdrawn.
        The App must also never have learned the end from a failed read.
        """
        opened = per_phase["open"]
        ended = per_phase["ended"]
        assert self._gatt is not None
        characteristic = self._gatt["uuid"]
        observed, properties = self._live_facts(opened)
        if "read" not in properties:
            raise RuntimeError(
                f"the declared {characteristic} is not readable: {properties}"
            )
        initial = self._value_read(opened, "initial-value", characteristic)
        observed.append("read:initial")
        # The host's own action: the driver record states it, and the App was told nothing.
        observed.append("link:dropped")

        announced = ended["projection"]
        self._assert_fact(announced, "announced", "link", "disconnected")
        self._assert_fact(announced, "announced", "linkEnd", "unrequested")
        settled = self._value(announced, "announced", "failure")
        if settled == "unknown":
            raise RuntimeError(
                "the announced end carries an unreadable failure identity"
            )
        observed.append("link:announced")

        after = self._reading(ended, "after")
        if self._value_read(ended, "after", characteristic) != initial:
            raise RuntimeError(
                f"a value appeared after the link ended: {initial!r} -> {after!r}"
            )
        silent = ended["projection"]
        self._assert_fact(silent, "silent", "link", "disconnected")
        self._assert_fact(silent, "silent", "linkEnd", "unrequested")
        if self._value(silent, "silent", "failure") != settled:
            raise RuntimeError(
                "the App changed its own account during the quiet window: "
                f"{settled!r} -> {self._value(silent, 'silent', 'failure')!r}"
            )
        self._require_quiet_step(ended)
        observed.append("link:silent")

        self._require_withdrawn_controls(ended)
        observed.append("controls:withdrawn")

        entries = self._ledger_entries(ended)
        if [action for _, action, outcome, _ in entries if outcome == "failed"]:
            raise RuntimeError(
                "the App recorded a failed action while observing the link's end: "
                "the end lane announces it, nothing else does"
            )
        self._require_ledger(ended, _PASSIVE_LEDGER)
        observed.append(self._fact(silent, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _require_quiet_step(self, result: dict[str, Any]) -> None:
        """The quiet window is a temporal claim, so the plan must have carried one."""
        for step in (result.get("receipt") or {}).get("steps") or []:
            planned = step.get("step") or {}
            if planned.get("op") == "still" and planned.get("id") == "run-status":
                if str((step.get("observed") or {}).get("still", "")) == "":
                    raise RuntimeError("the quiet window was not sampled")
                return
        raise RuntimeError("the plan carried no quiet window for the link's end")

    def _require_withdrawn_controls(self, result: dict[str, Any]) -> None:
        """Every control that needs a link must have been observed as withdrawn."""
        required = set(CONTROLS_REQUIRING_LINK)
        withdrawn = {
            str((step.get("step") or {}).get("id"))
            for step in (result.get("receipt") or {}).get("steps") or []
            if (step.get("step") or {}).get("op") == "wait"
            and (step.get("step") or {}).get("enabled") is False
        }
        missing = sorted(required - withdrawn)
        if missing:
            raise RuntimeError(
                f"the App still offers controls that need a live link: {missing}"
            )

    def _ccc_enable_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A refused subscription enable, then a retry that establishes and delivers.

        The refusal is the peer's answer to the CCC write, so the App must end that attempt as
        a *failed* subscription — never `subscribed`. An established subscription alone is not
        proof the lane works, so the host emits a value afterwards and the App's own pull has
        to return it.
        """
        opened = per_phase["open"]
        enable = per_phase["enable"]
        settle = per_phase["settle"]
        collect = per_phase["collect"]
        assert self._subscription is not None
        mode = self._subscription["mode"]
        observed, properties = self._live_facts(opened)
        capability = "indicate" if mode == "indication" else "notify"
        if capability not in properties:
            raise RuntimeError(
                f"the declared characteristic reports {properties}; this scene requires "
                f"{capability!r}"
            )

        # The App accepted the submission and its wait is outstanding: that is the ledger's
        # fact, below. The status must merely show that a notify state exists — requiring the
        # transient `enabling` here turned the scene into a race against the device's own
        # automation channel (2026-11-26: a 120.6 s press settled the read past that state,
        # and the scene failed while its real fact was intact).
        held = enable["projection"]
        if self._value(held, "held", "notify") == "none":
            raise RuntimeError(
                "the App published no notify state after the enable, so its handler did not run"
            )
        entries = self._ledger_entries(enable)
        tail = [(action, outcome) for _, action, outcome, _ in entries][-2:]
        if tail != [("notify-enable", "succeeded"), ("notify-enable-wait", "pending")]:
            raise RuntimeError(
                "the App did not record an accepted enable whose wait is outstanding: "
                f"{tail}"
            )
        observed.append("notify:held")

        rejected = settle["projection"]
        self._assert_fact(rejected, "rejected", "notify", "failed")
        identity = self._value(rejected, "rejected", "failure")
        if att_status_token(identity) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the refused subscription did not report the peer's ATT status "
                f"{REJECT_ATT_RESULT}: {identity!r}"
            )
        observed.append("notify:rejected")
        self._assert_fact(rejected, "recovered", "notify", "subscribed")
        self._assert_fact(rejected, "recovered", "notifyMode", mode)
        observed.append("notify:recovered")

        notified = collect["projection"]
        self._assert_fact(notified, "notified", "notifyMode", mode)
        if self._count(notified, "notified", "notifyTotal") < 1:
            raise RuntimeError("the App reported no notification value")
        observed.append("notify:value-matched")
        self._require_ccc_enable_ledger(collect)
        observed.append("notify:disabled")
        observed.append(
            self._fact(
                collect["projection"], "ended", "notify", "ended", "notify:ended"
            )
        )
        observed.append(
            self._fact(collect["projection"], "closed", "state", "closed", "closed")
        )
        return self._require_trace(observed)

    def _connect_failure_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A connect the peer could not answer, reported as a failure rather than a link.

        Three facts no plan step can express as a value: the App's own record has exactly one
        failed action and it is the connect's wait, the projection carries the same failure
        identity that record does, and neither a link nor an end is invented. The quiet
        window then holds all of it — a late `connected` or a rewritten failure breaks one.
        """
        opened = per_phase["open"]
        attempted = per_phase["attempt"]
        projection = opened["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
            self._fact(projection, "scanning", "scan", "running", "scan:running"),
            self._advertisement(opened),
            self._fact(projection, "stopped", "scan", "stopped", "scan:stopped"),
            # The host's own act: the driver record states it, and the App was told nothing.
            "peer:withdrawn",
        ]
        refused = attempted["projection"]
        entries = self._ledger_entries(attempted)
        self._require_connect_failure_ledger(entries)
        details = [
            detail
            for _, action, outcome, detail in entries
            if action == "connect-wait" and outcome == "failed"
        ]
        identity = _recorded_failure_identity(details[0])
        if identity is None:
            raise RuntimeError(
                f"the App's failed connect names no failure identity: {details[0]!r}"
            )
        self._assert_fact(refused, "refused", "failure", identity)
        observed.append("connect:refused")

        # Nothing began, so nothing ended: `linkEnd` must stay `none`, not be attributed to a
        # peer or an operator. A scene that only checked `link` would accept a failure that
        # also invented an end.
        self._assert_fact(refused, "refused", "link", "none")
        self._assert_fact(refused, "refused", "linkEnd", "none")
        observed.append("link:absent")

        silent = attempted["projection"]
        self._assert_fact(silent, "silent", "link", "none")
        self._assert_fact(silent, "silent", "linkEnd", "none")
        if self._value(silent, "silent", "failure") != identity:
            raise RuntimeError(
                "the App changed its own account of the failure during the quiet window: "
                f"{identity!r} -> {self._value(silent, 'silent', 'failure')!r}"
            )
        self._require_quiet_step(attempted)
        observed.append("link:silent")

        self._require_withdrawn_controls(attempted)
        observed.append("controls:withdrawn")

        observed.append(self._fact(silent, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _link_facts_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """Link facts whose answer may legitimately be "this platform cannot do that".

        The trace records that each question *was answered*, never that all four succeeded:
        CoreBluetooth has no MTU request, no connection priority and no attribute-cache
        invalidation, so a scene that required `done` would fail on one of the two phones for
        a platform fact. What it does require is that a refusal is a refusal — the platform's
        own `not_supported` code — and never a timeout, a hang or a bare failure.
        """
        result = per_phase[self._phases[0].name]
        projection = result["projection"]
        observed = [
            self._fact(projection, "initial", "state", "idle", "idle"),
            self._fact(projection, "started", "state", "started", "started"),
            self._fact(projection, "scanning", "scan", "running", "scan:running"),
            self._advertisement(result),
            self._fact(projection, "stopped", "scan", "stopped", "scan:stopped"),
            self._fact(projection, "linked", "link", "connected", "link:connected"),
        ]
        # A query is what every supported platform can answer; only the *limits* are portable
        # (CoreBluetooth reports no negotiated MTU at all), so they are what this asserts.
        observed.append(self._fact(projection, "mtu", "mtu", "done", "mtu:observed"))
        before = self._require_mtu_limits(self._value(projection, "mtu", "mtuWrites"))
        observed.append(
            self._require_answered(result, "mtuRequest", "mtu:request-answered")
        )
        # The request's effect, as a fact instead of a receipt: a platform that applied it
        # must report at least the limits it had before, and a platform that refused the
        # capability must report exactly the ones it had before.
        after = self._require_mtu_limits(
            self._value(projection, "mtuAfter", "mtuWrites")
        )
        requested = self._value(projection, "mtuRequest", "mtuRequest")
        if requested == "done":
            if any(later < earlier for earlier, later in zip(before, after)):
                raise RuntimeError(
                    "the MTU request settled but the platform reports smaller limits: "
                    f"{before} -> {after}"
                )
        elif after != before:
            raise RuntimeError(
                "the platform refused the MTU request yet the limits changed: "
                f"{before} -> {after}"
            )
        observed.append("mtu:re-queried")
        observed.append(self._require_answered(result, "priority", "priority:answered"))
        observed.append(
            self._require_answered(result, "refresh", "cache:refresh-answered")
        )
        entries = self._ledger_entries(result)
        self._require_link_facts_ledger(entries)
        observed.append(self._fact(projection, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _require_mtu_limits(self, writes: str) -> list[int]:
        """Both write limits, each at least the ATT default payload.

        The platform may report only the two limits and no MTU, so the limits are the fact a
        scene can hold across both phones. 20 bytes is the default-ATT floor (MTU 23), not a
        negotiated value: a platform reporting less would be reporting something unusable.
        """
        if _MTU_LIMITS.fullmatch(writes) is None:
            raise RuntimeError(
                "the MTU query reported no usable <withResponse>/<withoutResponse>: "
                f"{writes!r}"
            )
        limits = [int(part) for part in writes.split("/") if part.isdigit()]
        if not limits or min(limits) < 20:
            raise RuntimeError(
                f"the MTU query reported an unusable payload limit: {writes!r}"
            )
        return limits

    def _require_answered(
        self, result: dict[str, Any], key: str, observation: str
    ) -> str:
        """One link fact settled, or was refused as a capability this platform lacks."""
        projection = result["projection"]
        value = self._value(projection, key, key)
        failure = self._value(projection, key, "failure")
        if value == "done":
            if failure != "none":
                raise RuntimeError(
                    f"the {key} lane settled but the run reports a failure: {failure!r}"
                )
            return observation
        if value == "unsupported":
            if not is_not_supported(failure):
                raise RuntimeError(
                    f"the {key} lane says unsupported but its failure is {failure!r}; "
                    "only the platform's own refusal may end it as unsupported"
                )
            return observation
        raise RuntimeError(
            f"the {key} lane answered {value!r}; it must settle or be refused as unsupported"
        )

    def _require_link_facts_ledger(
        self, entries: list[tuple[int, str, str, str]]
    ) -> None:
        """The App's own record: one submission and one wait per fact, nothing else.

        A refused capability legitimately records a *failed* wait, so this cannot use the
        all-succeeded template. The failures are still constrained: each one names the
        platform's `not_supported` code, and no other action may fail.
        """
        names = _collapse([(action, outcome) for _, action, outcome, _ in entries])
        if names != list(LINK_FACTS_LEDGER):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{names} != {list(LINK_FACTS_LEDGER)}"
            )
        failures = [
            (action, detail)
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
        ]
        unexplained = [
            action for action, detail in failures if not is_not_supported(detail)
        ]
        if unexplained:
            raise RuntimeError(
                "a link fact failed for a reason other than an unsupported capability: "
                f"{unexplained}"
            )
        if entries[-1][1] != "close":
            raise RuntimeError("the App's record does not end with the close")

    def _require_connect_failure_ledger(
        self, entries: list[tuple[int, str, str, str]]
    ) -> None:
        """The App's own record: one accepted connect submission and one failed wait.

        This is the one managed scene whose record must contain a failure, so it cannot use
        the all-succeeded template. Everything else stays strict: the failed action is the
        connect's wait and nothing else, its submission was accepted (a refused submission
        would never have been put on the wire), the record holds no other action at all, and
        it ends with the close.
        """
        names = _collapse([(action, outcome) for _, action, outcome, _ in entries])
        if names != list(CONNECT_FAILURE_LEDGER):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{names} != {list(CONNECT_FAILURE_LEDGER)}"
            )
        failures = [
            (action, detail)
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
        ]
        if [action for action, _ in failures] != ["connect-wait"]:
            raise RuntimeError(
                "the App's only failed action must be the connect's wait: "
                f"{[action for action, _ in failures]}"
            )
        if not failures[0][1]:
            raise RuntimeError("the App's failed connect wait carries no detail")
        if entries[-1][1] != "close":
            raise RuntimeError("the App's record does not end with the close")

    def _require_ccc_enable_ledger(self, result: dict[str, Any]) -> None:
        """One refused enable, one accepted retry, one value pulled, one disable."""
        entries = self._ledger_entries(result)
        failed = [
            (action, detail)
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
        ]
        refused = [
            detail for action, detail in failed if action == "notify-enable-wait"
        ]
        if len(failed) != 1 or len(refused) != 1:
            raise RuntimeError(
                f"the refused enable is not the App's only failure: {failed}"
            )
        if att_status_token(refused[0]) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the App's failed enable wait does not name the peer's ATT status "
                f"{REJECT_ATT_RESULT}: {refused[0]!r}"
            )
        by_action = {
            action: [outcome for _, entry, outcome, _ in entries if entry == action]
            for action in ("notify-enable", "notify-enable-wait", "notify-disable")
        }
        if by_action["notify-enable"] != ["succeeded", "succeeded"]:
            raise RuntimeError(
                "the App did not record two accepted enables: "
                f"{by_action['notify-enable']}"
            )
        if sorted(by_action["notify-enable-wait"]) != ["failed", "succeeded"]:
            raise RuntimeError(
                "the App's enable waits are not this scene: one refused, one accepted: "
                f"{by_action['notify-enable-wait']}"
            )
        if by_action["notify-disable"] != ["succeeded"]:
            raise RuntimeError(
                f"the App did not record one accepted disable: {by_action['notify-disable']}"
            )
        if entries[-1][1] != "close":
            raise RuntimeError("the App's record does not end with the close")

    def _ccc_disable_observations(
        self, per_phase: dict[str, dict[str, Any]]
    ) -> list[str]:
        """A refused *disable*: the stream must still be alive, then a real disable ends it.

        A refused CCC write leaves the peer enabled, so a value is still possible; a platform
        may also have stopped delivering locally. Both are legitimate, and the scene records
        the probe rather than its platform-specific outcome: what it does hold the App to is
        that the subscription never claims to have ended, and that a value it does receive is
        one the host actually emitted.
        """
        opened = per_phase["open"]
        subscribe = per_phase["subscribe"]
        value = per_phase["value"]
        hold = per_phase["hold"]
        probe = per_phase["probe"]
        assert self._subscription is not None
        mode = self._subscription["mode"]
        observed, properties = self._live_facts(opened)
        capability = "indicate" if mode == "indication" else "notify"
        if capability not in properties:
            raise RuntimeError(
                f"the declared characteristic reports {properties}; this scene requires "
                f"{capability!r}"
            )
        # A disable needs an enabled subscription, and the value the host emits afterwards is
        # what proves the lane works — neither is taken from the other's phase.
        subscribed = subscribe["projection"]
        self._assert_fact(subscribed, "subscribed", "notify", "subscribed")
        self._assert_fact(subscribed, "subscribed", "notifyMode", mode)
        observed.append("notify:subscribed")

        notified = value["projection"]
        self._assert_fact(notified, "notified", "notifyMode", mode)
        if self._count(notified, "notified", "notifyTotal") < 1:
            raise RuntimeError("the App reported no notification value")
        observed.append("notify:value-matched")

        entries = self._ledger_entries(hold)
        tail = [(action, outcome) for _, action, outcome, _ in entries][-2:]
        if tail != [
            ("notify-disable", "succeeded"),
            ("notify-disable-wait", "pending"),
        ]:
            raise RuntimeError(
                "the App did not record an accepted disable whose wait is outstanding: "
                f"{tail}"
            )
        observed.append("notify:held")

        rejected = probe["projection"]
        self._assert_fact(rejected, "rejected", "notify", "unverified")
        identity = self._value(rejected, "rejected", "failure")
        if att_status_token(identity) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the refused disable did not report the peer's ATT status "
                f"{REJECT_ATT_RESULT}: {identity!r}"
            )
        observed.append("notify:rejected")

        branch = self._wait_any_branch(probe)
        probed = probe["projection"]
        if branch == 0:
            # The platform kept delivering: the App must say so, and the value is its own.
            self._assert_fact(probed, "probed", "notify", "subscribed")
        elif branch == 1:
            # The platform stopped delivering locally: the App may only say it does not know.
            self._assert_fact(probed, "probed", "notify", "unverified")
        else:
            raise RuntimeError(f"the probe reported an unknown branch {branch}")
        self._require_ccc_disable_ledger(probe, lost=branch == 1)
        observed.append("notify:probed")
        # The second, unhidden disable is what retires the subscription; the ledger check
        # above required it to settle its own wait and the stream to keep delivering nothing.
        observed.append("notify:recovered")

        self._assert_fact(probed, "ended", "notify", "ended")
        observed.append("notify:ended")
        observed.append(self._fact(probed, "closed", "state", "closed", "closed"))
        return self._require_trace(observed)

    def _require_ccc_disable_ledger(
        self, result: dict[str, Any], *, lost: bool
    ) -> None:
        """One refused disable, one value, the probe, then a disable that settles.

        A probe that found the lane stopped delivering is the only case where a second
        failure is legitimate, and the App must name it as an *observation* failure: a
        timeout is not proof of an end.
        """
        entries = self._ledger_entries(result)
        failed = [
            (action, detail)
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
        ]
        refused = [
            detail for action, detail in failed if action == "notify-disable-wait"
        ]
        expected_failures = ["notify-disable-wait"] + (["notify-pull"] if lost else [])
        if [action for action, _ in failed] != expected_failures:
            raise RuntimeError(
                f"the App recorded failures this scene did not cause: {failed}"
            )
        if att_status_token(refused[0]) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the App's failed disable wait does not name the peer's ATT status "
                f"{REJECT_ATT_RESULT}: {refused[0]!r}"
            )
        if lost:
            probe_detail = failed[1][1]
            if not probe_detail.startswith("observation/"):
                raise RuntimeError(
                    "the probe's failure is not an observation failure: "
                    f"{probe_detail!r}"
                )
        outcomes = {
            action: [outcome for _, entry, outcome, _ in entries if entry == action]
            for action in ("notify-enable", "notify-disable")
        }
        if outcomes["notify-enable"] != ["succeeded"]:
            raise RuntimeError(
                f"the App recorded {outcomes['notify-enable']} enables; this scene is one"
            )
        if outcomes["notify-disable"] != ["succeeded", "succeeded"]:
            raise RuntimeError(
                "the App did not record two accepted disables: "
                f"{outcomes['notify-disable']}"
            )
        if [
            outcome
            for _, action, outcome, _ in entries
            if action == "notify-disable-wait"
        ].count("succeeded") != 1:
            raise RuntimeError("the recovered disable did not settle its own wait")
        if entries[-1][1] != "close":
            raise RuntimeError("the App's record does not end with the close")

    def _wait_any_branch(self, result: dict[str, Any]) -> int:
        """Which condition of the scene's one disjunction held, as the driver reported it."""
        for step in (result.get("receipt") or {}).get("steps") or []:
            if (step.get("step") or {}).get("op") != "waitAny":
                continue
            reported = str((step.get("observed") or {}).get("waitAny", ""))
            if not reported.isdigit():
                raise RuntimeError("the scene receipt has no disjunction result")
            return int(reported)
        raise RuntimeError("the scene receipt has no disjunction step")

    def _require_write_reject_ledger(self, result: dict[str, Any]) -> None:
        """The App's own record: one refused write, in the sequence this scene ran."""
        entries = self._require_collapsed(result, WRITE_REJECT_LEDGER)
        failed = [
            (action, detail)
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
        ]
        if [action for action, _ in failed] != ["gatt-write-wait"]:
            raise RuntimeError(
                f"the refused write is not the App's only failure: {failed}"
            )
        if att_status_token(failed[0][1]) != str(REJECT_ATT_RESULT):
            raise RuntimeError(
                "the App's failed write wait does not name the peer's ATT status "
                f"{REJECT_ATT_RESULT}: {failed[0][1]!r}"
            )

    def _require_collapsed(
        self, result: dict[str, Any], expected: tuple[str, ...]
    ) -> list[tuple[int, str, str, str]]:
        entries = self._ledger_entries(result)
        names = _collapse([(action, outcome) for _, action, outcome, _ in entries])
        if names != list(expected):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{names} != {list(expected)}"
            )
        return entries

    def _require_cancellation_ledger(self, result: dict[str, Any]) -> None:
        """The App's own record: three reads, exactly one cancelled, one close.

        This scene is the one whose record legitimately contains a failed action (the
        cancelled wait), so it cannot reuse the all-succeeded template. Everything else is
        still strict: the cancelled *wait* is the only failure, its detail names the
        cancellation, and the read side is the same sequence every read/write scene records.
        """
        entries = self._ledger_entries(result)
        # Repeats are collapsed exactly like every other scene: how many pulls a scan needs
        # is a property of the RF environment, not of the scene.
        names = _collapse([(action, outcome) for _, action, outcome, _ in entries])
        head = len(CANCELLATION_LEDGER_PREFIX)
        if tuple(names[:head]) != CANCELLATION_LEDGER_PREFIX:
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{names} does not start with {list(CANCELLATION_LEDGER_PREFIX)}"
            )
        # A cancel request and the wait it retires settle independently, so both orders are
        # this scene's own record; nothing else about the rest is negotiable.
        if tuple(names[head:]) not in CANCELLATION_LEDGER_TAILS:
            raise RuntimeError(
                f"the App's recorded reads are not this scene: {names[head:]}"
            )
        reads = [
            (action, outcome, detail)
            for _, action, outcome, detail in entries
            if action in CANCELLATION_READ_ACTIONS
        ]
        submissions = [outcome for action, outcome, _ in reads if action == "gatt-read"]
        if submissions != ["succeeded", "succeeded", "succeeded"]:
            raise RuntimeError(
                f"the App did not record three accepted read submissions: {submissions}"
            )
        waits = [
            (outcome, detail)
            for action, outcome, detail in reads
            if action == "gatt-read-wait"
        ]
        succeeded = [detail for outcome, detail in waits if outcome == "succeeded"]
        cancelled = [
            detail
            for outcome, detail in waits
            if outcome == "failed" and "cancelled" in detail
        ]
        if len(waits) != 3 or len(succeeded) != 2 or len(cancelled) != 1:
            raise RuntimeError(
                "the App's read waits are not this scene: two settled and one cancelled, "
                f"observed {waits}"
            )
        cancels = [
            outcome for action, outcome, _ in reads if action == "gatt-read-cancel"
        ]
        if cancels != ["succeeded"]:
            raise RuntimeError(
                f"the App did not record exactly one accepted cancellation: {cancels}"
            )
        unexpected = [
            action
            for _, action, outcome, _ in entries
            if outcome != "succeeded" and action != "gatt-read-wait"
        ]
        if unexpected:
            raise RuntimeError(f"the App recorded an unexpected failure: {unexpected}")
        if entries[-1][1] != "close":
            raise RuntimeError("the App's record does not end with the close")

    def _ledger_entries(
        self, result: dict[str, Any]
    ) -> list[tuple[int, str, str, str]]:
        return self._ledger_frame(result)[1]

    def _ledger_frame(
        self, result: dict[str, Any]
    ) -> tuple[int, list[tuple[int, str, str, str]]]:
        """The App's bounded record as `(omitted, entries)`.

        `omitted` states how many actions the App's one-screen window dropped. A single-scene
        derivation needs the *whole* session, so it must not compare a suffix and call it the
        record; the recovery scene, which deliberately spans one window, reads the same frame
        and says what its suffix claim is.
        """
        text = (result.get("receipt") or {}).get("values", {}).get("ledger")
        if type(text) is not str:
            raise RuntimeError("the scene receipt has no action ledger")
        try:
            return action_ledger.parse(text)
        except action_ledger.ActionLedgerError as error:
            raise RuntimeError(
                f"the action ledger is not this contract: {error}"
            ) from error

    @staticmethod
    def _reading(result: dict[str, Any], name: str) -> str:
        """One outcome the plan asked the App to read, as the App rendered it."""
        text = (result.get("receipt") or {}).get("values", {}).get(name)
        if type(text) is not str or not text:
            raise RuntimeError(f"the scene receipt has no {name!r} reading")
        return text

    @classmethod
    def _value_read(cls, result: dict[str, Any], name: str, characteristic: str) -> str:
        """One characteristic's rendered bytes, decoded through the App's own codec.

        A result about another characteristic, or a receipt that is not a value at all, is
        refused here: "something was rendered" is not "this characteristic reported a
        value".
        """
        text = cls._reading(result, name)
        prefix = characteristic.casefold() + ": "
        if not text.casefold().startswith(prefix):
            raise RuntimeError(
                f"the {name} reading names another characteristic: {text!r}"
            )
        payload = text[len(prefix) :]
        if not GATT_VALUE_TEXT.fullmatch(payload):
            raise RuntimeError(f"the {name} reading is not a value rendering: {text!r}")
        return payload

    @staticmethod
    def _value(projection: dict[str, dict[str, str]], read: str, key: str) -> str:
        frame = projection.get(read)
        if type(frame) is not dict or key not in frame:
            raise RuntimeError(f"the scene receipt has no {read}.{key}")
        return str(frame[key])

    @classmethod
    def _assert_fact(
        cls,
        projection: dict[str, dict[str, str]],
        read: str,
        key: str,
        expected: str,
    ) -> None:
        value = cls._value(projection, read, key)
        if value != expected:
            raise RuntimeError(f"{read}.{key} is {value!r}, not {expected!r}")

    @classmethod
    def _fact(
        cls,
        projection: dict[str, dict[str, str]],
        read: str,
        key: str,
        expected: str,
        observation: str,
    ) -> str:
        cls._assert_fact(projection, read, key, expected)
        return observation

    @staticmethod
    def _count(projection: dict[str, dict[str, str]], read: str, key: str) -> int:
        frame = projection.get(read)
        if type(frame) is not dict or not str(frame.get(key, "")).isdigit():
            raise RuntimeError(f"the scene receipt has no numeric {read}.{key}")
        return int(str(frame[key]))

    def _advertisement(self, result: dict[str, Any]) -> str:
        steps = result["receipt"]["steps"]
        for step in steps:
            if (step.get("step") or {}).get("op") != "tapUntil":
                continue
            seen = str((step.get("observed") or {}).get("scan-latest", ""))
            if "exists label=" in seen:
                name = self._expected_local_name
                if name is not None and f'name="{name}"' not in seen:
                    raise RuntimeError(
                        "the advertisement this scene matched does not name the fixture: "
                        + seen[:200]
                    )
                return "scan:advertisement-matched"
        raise RuntimeError("the scene never rendered an advertisement")

    def _require_descriptor_io_ledger(self, result: dict[str, Any]) -> None:
        """The descriptor scene's record, with the one refusal a platform may give.

        CoreBluetooth refuses a direct CCCD write as its own `not_supported`, and the refusal
        can land at either boundary: a submission the engine rejects before queueing (no wait
        row exists) or a terminal the backend reports for the queued operation. Both are the
        platform's answer; a refusal with any other code, or any other failed action, is not.
        """
        entries = self._ledger_entries(result)
        refusals = [
            action
            for _, action, outcome, detail in entries
            if outcome != "succeeded"
            and action in ("descriptor-write", "descriptor-write-wait")
            and is_not_supported(detail)
        ]
        failed = [
            action
            for _, action, outcome, detail in entries
            if outcome != "succeeded" and action not in refusals
        ]
        if failed:
            raise RuntimeError(f"the App recorded a failed action: {failed}")
        if len(refusals) > 1:
            raise RuntimeError(
                f"the descriptor write was refused twice, which is not one answer: {refusals}"
            )
        omitted, _ = self._ledger_frame(result)
        if omitted:
            raise RuntimeError(
                f"the App's bounded action window dropped {omitted} action(s), so this scene's "
                "record is incomplete"
            )
        expected = DESCRIPTOR_IO_LEDGER
        if refusals == ["descriptor-write"]:
            # Refused before anything was queued, so no wait row can exist.
            expected = tuple(
                name for name in expected if name != "descriptor-write-wait"
            )
        collapsed = _collapse_retries(
            _collapse([(action, outcome) for _, action, outcome, _ in entries])
        )
        if (
            len(collapsed) == len(expected) + 1
            and collapsed[-2] == "notify-pull"
            and collapsed[-1] == "close"
        ):
            collapsed = collapsed[:-2] + ["close"]
        if collapsed != list(expected):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{collapsed} != {list(expected)}"
            )

    def _require_ledger(
        self, result: dict[str, Any], template: tuple[str, ...]
    ) -> list[str]:
        entries = self._ledger_entries(result)
        failed = [action for _, action, outcome, _ in entries if outcome != "succeeded"]
        if failed:
            raise RuntimeError(f"the App recorded a failed action: {failed}")
        omitted, _ = self._ledger_frame(result)
        if omitted:
            # The record is one bounded window; a scene that expects the whole session must
            # fail here rather than compare a suffix and silently ignore what was dropped.
            raise RuntimeError(
                f"the App's bounded action window dropped {omitted} action(s), so this scene's "
                "record is incomplete"
            )
        collapsed = _collapse_retries(
            _collapse([(action, outcome) for _, action, outcome, _ in entries])
        )
        # The App may record one more pull after the disable: it is the pull that consumes
        # the terminal the disable produced.
        if (
            len(collapsed) == len(template) + 1
            and collapsed[-2] == "notify-pull"
            and collapsed[-1] == "close"
        ):
            collapsed = collapsed[:-2] + ["close"]
        if collapsed != list(template):
            raise RuntimeError(
                "the App's recorded actions do not describe this scene: "
                f"{collapsed} != {list(template)}"
            )
        return collapsed

    def _require_trace(self, observed: list[str]) -> list[str]:
        if observed != list(self._scene.trace):
            raise RuntimeError(
                f"native observations do not describe the {self._scene.name} scene: "
                f"{observed}"
            )
        return observed

    # -- witnesses ------------------------------------------------------------

    def _publish(
        self,
        raw: Path,
        scene: SceneEvidence,
        per_phase: dict[str, dict[str, Any]],
        failure: BaseException | None,
    ) -> None:
        """Write the driver's witnesses; a failed scene still explains itself."""
        phases: list[dict[str, Any]] = []
        published_once: set[str] = set()
        for phase in self._phases:
            result = per_phase.get(phase.name)
            entry: dict[str, Any] = {
                "name": phase.name,
                "host": phase.host,
                "steps": len(self._steps(phase)),
                "result": None,
                "status": "failed",
                "step_seconds_total": None,
                **self._phase_extra(result or {}),
            }
            if result is not None:
                if self.one_process and self._run_document is not None:
                    # One process ran the whole scene: publish that run once, then each
                    # phase's own slice, so a reviewer sees both the run and the view a
                    # derivation consumed.
                    run_name = f"scene-{_slug(self._scene.name)}.json"
                    if run_name not in published_once:
                        publish_new_json(self._output / run_name, self._run_document)
                        published_once.add(run_name)
                published = f"phase-{_slug(phase.name)}.json"
                receipt_name = f"receipt-{_slug(phase.name)}.json"
                if published not in published_once:
                    publish_new_json(self._output / published, result)
                    if isinstance(result.get("receipt"), dict):
                        publish_new_json(self._output / receipt_name, result["receipt"])
                    published_once.add(published)
                entry.update(
                    result=published,
                    status=result.get("status", "failed"),
                    step_seconds_total=result.get("step_seconds_total"),
                )
                self._publish_diagnostics(raw, phase, result)
            phases.append(entry)
        record = {
            "schema_version": 1,
            "scope": self.record_scope,
            "run_id": self._run_id,
            "kind": self.kind,
            "session_id": self.session,
            "device": self._device,
            "app_id": self._app_id,
            **self._artifact(),
            "plan": scene.name,
            "phases": phases,
            "steps": sum(entry["steps"] for entry in phases),
            "status": "failed" if failure is not None else "passed",
            "primary_error": None
            if failure is None
            else "".join(format_exception_only(failure)).strip(),
        }
        publish_new_json(self._output / "record.json", record)

    # -- retirement -----------------------------------------------------------

    def require_closed(self) -> None:
        """Nothing resident: every phase returns only after its driver run has exited."""

    def close(self, deadline: float) -> None:
        """Nothing to retire; the driver runs are over before `drive` returns."""


__all__ = ["PHASES", "PLANNED_SCENES", "NativeSceneDriver", "Phase"]
