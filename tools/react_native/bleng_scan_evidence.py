"""Sealing and offline replay for the formal managed scan bundle.

This module audits host-submitted evidence only. It never acquires a phone or
fixture lease, starts a provider, talks to an SDK, or upgrades the pending host
gate. The runner owns those live decisions; this module binds their recorded
receipts into one movable, hash-covered bundle. Both platforms seal through the
same stages; the platform-specific candidate and preparation facts are validated
by their own functions rather than by tolerating each other's fields.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import fixture_session

from . import evidence_files, evidence_io
from .capture_io import publish_new_json
from .ios_radio_lease import SCAN_OBSERVATION_SCOPE

SCOPE = "formal-android-managed-scan"
MAX_SECONDS = 120
MAX_JSON_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class SceneEvidence:
    """One managed UI scene's evidence vocabulary.

    A scene is *what the App was driven through*, so its identity is exactly the triple a
    bundle must state: the outer producer scope, the inner prepared scope and the ordered
    observation trace that can support a pass. The inner runner's output directory stays
    `scan/` for both scenes: it names the borrowed prepared run, not the scene, and renaming
    it would be an evidence-format change.

    Both scopes are derived from the scene's name instead of being restated per scene, and the
    outer one also names the **platform**: the producer is one platform's managed lane, so an
    iPhone bundle that called itself `formal-android-managed-*` was claiming a producer that
    did not produce it.
    """

    name: str
    rf: str
    trace: tuple[str, ...]
    # The fixture profile role the scene subscribes to; its properties decide the mode.
    characteristic_role: str | None = None
    # Which UI exercise this scene owns. It is stated rather than inferred from
    # `characteristic_role`, because the io scene declares a characteristic too.
    kind: str = "scan"
    # Cycles a repeating scene runs. The trace is derived from this same number, so a
    # scene cannot advertise three cycles and drive ten.
    cycles: int | None = None
    # The shortest scenario budget this scene can finish in. A run that was handed less
    # refuses before it starts driving instead of failing in cycle seven, and the number is
    # a measured host estimate, not a gate.
    min_seconds: int | None = None

    def scope(self, platform: str) -> str:
        """The outer producer scope for one platform's managed lane."""
        if platform not in MANAGED_PLATFORMS:
            raise ValueError(f"unknown managed platform {platform!r}")
        return f"formal-{platform}-managed-{self.name}"

    @property
    def inner_scope(self) -> str:
        """The prepared run's own scope; platform-agnostic because the run is shared."""
        return f"formal-rn-prepared-{self.name}"


SCAN_SCENE = SceneEvidence(
    name="scan",
    rf="scan-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "closed",
    ),
)
_SUBSCRIPTION_TRACE = (
    "idle",
    "started",
    "scan:running",
    "scan:advertisement-matched",
    "scan:stopped",
    "link:connected",
    "gatt:ready",
    "notify:subscribed",
    "notify:value-matched",
    "notify:disabled",
    "notify:ended",
    "closed",
)
NOTIFICATION_SCENE = SceneEvidence(
    name="notification",
    rf="notification-ui-observation",
    trace=_SUBSCRIPTION_TRACE,
    characteristic_role="updates",
    kind="subscription",
)
INDICATION_SCENE = SceneEvidence(
    name="indication",
    rf="indication-ui-observation",
    trace=_SUBSCRIPTION_TRACE,
    characteristic_role="indications",
    kind="subscription",
)
# The App's own byte rendering (`binary()` in `src/useManualScan.ts`): a count, at most 16
# lowercase bytes, and ` …` inside the brackets when the value was truncated. The count can
# exceed the shown bytes, so a long peer value still is one value. This is the single codec
# for both UI lanes: a scene may only claim a value it can decode through this shape, and a
# write receipt (which renders `wrote N bytes (type)`) is never mistaken for one.
GATT_VALUE_TEXT = re.compile(
    r"[0-9]{1,3} bytes \[(?:[0-9a-f]{2}(?: [0-9a-f]{2})*(?: …)?)?\]"
)


def render_gatt_value(payload: bytes) -> str:
    """The App's own rendering of one byte payload, so a scene can state an expectation.

    A scene that claims "the peer now holds what I wrote" has to compare against the App's
    rendering, not against a second formatting rule that could drift from `binary()`.
    """
    shown = " ".join(f"{byte:02x}" for byte in payload[:16])
    suffix = " …" if len(payload) > 16 else ""
    return f"{len(payload)} bytes [{shown}{suffix}]"


# A failure rendering names the platform status twice, in two shapes: the projection's
# identity (`apple/14`) and the sentence a receipt records (`gatt/…: … [apple/14 Name: …]`).
# Both name the status the platform reported, so one reader serves both.
_ATT_IDENTITY = re.compile(r"[A-Za-z]+/([^/\s]+)")
_ATT_MARKER = re.compile(r"\[([^/\]\s:]+)/([^\]\s:]+)")


def att_status_token(text: str) -> str | None:
    """The platform status one failure rendering carries, or None if it carries none."""
    identity = _ATT_IDENTITY.fullmatch(text)
    if identity is not None:
        return identity[1]
    marker = _ATT_MARKER.search(text)
    return marker[2] if marker is not None else None


# Read, write and read back one characteristic on the live link, then read RSSI. The
# receipt of a write is not a stored value, so the same characteristic is read back.
IO_SCENE = SceneEvidence(
    name="io",
    rf="io-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "io:read-initial",
        "io:written",
        "io:readback",
        "rssi:observed",
        "closed",
    ),
    characteristic_role="read-write",
    kind="io",
)
# One queued read cancelled while the peer holds the active one, then a normal recovery
# read. The hold is a host/fixture fact; every item below is what the App published.
CANCELLATION_SCENE = SceneEvidence(
    name="cancellation",
    rf="cancellation-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "read:held",
        "read:queued",
        "cancel:queued-accepted",
        "read:active-completed",
        "read:recovered",
        "closed",
    ),
    characteristic_role="read-write",
    kind="cancellation",
)
# A write the peer refuses inside the firmware: the App's own failed wait, no write
# receipt, an unchanged read-back, then the same write succeeding with a read-back equal
# to the request. The refusal is the peer's; every item below is what the App published.
WRITE_REJECT_SCENE = SceneEvidence(
    name="write-reject",
    rf="write-reject-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "read:initial",
        "write:held",
        "write:rejected",
        "value:unchanged",
        "write:recovered",
        "value:stored",
        "closed",
    ),
    characteristic_role="read-write",
    kind="write-reject",
)
# A subscription enable the peer refuses, then a normal enable that really delivers.
CCC_ENABLE_REJECT_SCENE = SceneEvidence(
    name="ccc-enable-reject",
    rf="ccc-enable-reject-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "notify:held",
        "notify:rejected",
        "notify:recovered",
        "notify:value-matched",
        "notify:disabled",
        "notify:ended",
        "closed",
    ),
    characteristic_role="updates",
    kind="ccc-enable-reject",
)
# A subscription disable the peer refuses. The peer keeps its CCC enabled, so the App must
# not claim a stopped subscription — and it must not claim an established one either: what
# happens to delivery is a platform fact the scene probes once. The trace records the probe,
# not its platform-specific outcome.
CCC_DISABLE_REJECT_SCENE = SceneEvidence(
    name="ccc-disable-reject",
    rf="ccc-disable-reject-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "notify:subscribed",
        "notify:value-matched",
        "notify:held",
        "notify:rejected",
        "notify:probed",
        "notify:recovered",
        "notify:ended",
        "closed",
    ),
    characteristic_role="updates",
    kind="ccc-disable-reject",
)
# One peer-initiated end: the App must announce it by itself from the connection's own
# end lane, attribute it to nobody on this screen, keep that reason over a stated quiet
# window and stop offering commands that need the link. A host action, App testimony, then
# silence — with no operator command anywhere in between.
PASSIVE_DISCONNECT_SCENE = SceneEvidence(
    name="passive-disconnect",
    rf="passive-disconnect-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "read:initial",
        "link:dropped",
        "link:announced",
        "link:silent",
        "controls:withdrawn",
        "closed",
    ),
    characteristic_role="read-write",
    kind="passive-disconnect",
)


# One end this App itself submitted, after a live subscription has been proven to deliver.
#
# Three separate claims follow from the operator's own receipt: the end is attributed to the
# operator rather than to nobody (`linkEnd=requested`, the package's own `BlengEndReason`),
# the link-dependent controls are
# withdrawn, and the subscription does not survive its link — a phase still saying
# `subscribed` on a dead link would be a claim the App cannot honour. The quiet window then
# holds all three facts at once, which is what separates "the App reported the end" from
# "the App is still reporting the end it reported".
DISCONNECT_SCENE = SceneEvidence(
    name="disconnect",
    rf="disconnect-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "read:initial",
        "notify:subscribed",
        "notify:value-matched",
        "link:end-requested",
        "notify:ended",
        "link:silent",
        "controls:withdrawn",
        "closed",
    ),
    characteristic_role="updates",
    kind="disconnect",
)

# A subscription that outlives a background/foreground cycle.
#
# The host emits while the App is away, so that value can only be observed after the App
# returns — which is what separates "the subscription survived" from "a second one was
# established". iOS declares `bluetooth-central` and Android runs a connected-device
# foreground service while the App is in the background; this scene is the claim those two
# capabilities exist to support.
BACKGROUND_SCENE = SceneEvidence(
    name="background",
    rf="background-subscription-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "notify:subscribed",
        "notify:value-matched",
        "notify:backgrounded",
        "notify:resumed",
        "notify:value-after-resume",
        "closed",
    ),
    characteristic_role="updates",
    kind="background",
    # The scene declares its own floor because part of it is a *held* window rather than an
    # operation: `BACKGROUND_DWELL_SECONDS` of being away, plus the two link phases around it.
    min_seconds=120,
)


# A connect the peer cannot answer: the scan matched the fixture, the host then withdrew its
# advertising, and the App asked to connect anyway. The claim is not "connect fails" — it is
# that the App reports the failure *as a failure*: no link is claimed, no end is invented, the
# failure identity is the one its own record carries, and every control that needs a link is
# withdrawn over a quiet window. A scene that accepted "still connecting" would pass on a
# timeout that never settled.
CONNECT_FAILURE_SCENE = SceneEvidence(
    name="connect-failure",
    rf="connect-failure-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "peer:withdrawn",
        "connect:refused",
        "link:absent",
        "link:silent",
        "controls:withdrawn",
        "closed",
    ),
    kind="connect-failure",
)


# Facts about the link itself, from capabilities a platform may legitimately not have: MTU
# query/request, connection priority and GATT-cache invalidation.
#
# The claim is not "they all work". It is that each one's answer is *surfaced as itself*:
# either the operation settled (`done`) or the platform said it has no such capability
# (`unsupported`, carrying the platform's own refusal code). A scene that accepted `failed`
# or `pending` here would hide exactly the bug this vocabulary exists to catch.
LINK_FACTS_SCENE = SceneEvidence(
    name="link-facts",
    rf="link-facts-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "mtu:observed",
        "mtu:request-answered",
        "mtu:re-queried",
        "priority:answered",
        "cache:refresh-answered",
        "closed",
    ),
    kind="link-facts",
)


# The peer's own descriptor state, read as a second witness for a subscription.
#
# The App says `subscribed`; the CCCD the peer stores says whether its notifications are
# enabled. Reading it is a separate command with its own terminal, and the scene correlates
# the two in both directions: enable -> `01 00`, disable -> `00 00`. Writing back the value
# the peer just reported proves the write path without changing the state the lane owns —
# where the platform permits it: CoreBluetooth refuses a direct CCCD write (it demands
# `setNotifyValue(_:for:)`), so `cccd:write-answered` is the marker both platforms reach and
# the derivation pins which answer each one gave.
DESCRIPTOR_IO_SCENE = SceneEvidence(
    name="descriptor-io",
    rf="descriptor-io-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "gatt:ready",
        "cccd:initial",
        "notify:subscribed",
        "cccd:enabled",
        "notify:disabled",
        "cccd:cleared",
        "cccd:write-answered",
        "cccd:unchanged",
        "closed",
    ),
    characteristic_role="updates",
    kind="descriptor-io",
)


# The platform's own inventory, checked against the link this run actually holds.
#
# A query that returns a list proves only that the command was accepted. The claim here is
# stronger: the `connected` inventory contains the peer the App is linked to, matched by the
# owner-scoped key the bridge documents as comparable with `peripheral.key()`. That makes
# `beginDeviceQuery` a fact about the adapter, not a self-report the App could fabricate.
DEVICES_QUERY_SCENE = SceneEvidence(
    name="devices-query",
    rf="devices-query-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "link:connected",
        "devices:listed",
        "devices:peer-connected",
        "closed",
    ),
    kind="devices-query",
)


# A standing "connect when the peer appears" request, proved against an absent peer.
#
# The trace says the platform *answered*, never that it linked: Apple has no standing-connect
# capability and refuses with its own code. The derivation pins which answers are acceptable.
AUTO_CONNECT_SCENE = SceneEvidence(
    name="auto-connect",
    rf="auto-connect-ui-observation",
    trace=(
        "idle",
        "started",
        "scan:running",
        "scan:advertisement-matched",
        "scan:stopped",
        "peer:withdrawn",
        "auto:answered",
        "closed",
    ),
    kind="auto-connect",
)


# The two inventories the observation main line never needs. Neither platform is asked to
# own a peer here: the claim is that each answer is the platform's own — an inventory or its
# refusal — never a timeout standing in for either.
DEVICES_KINDS_SCENE = SceneEvidence(
    name="devices-kinds",
    rf="devices-kinds-ui-observation",
    trace=(
        "idle",
        "started",
        "devices:bonded-answered",
        "devices:companion-answered",
        "closed",
    ),
    kind="devices-kinds",
)

# Radio power, asked in both directions. The marker says the *request* was answered; which
# answer it got is the derivation's business, because the two platforms (and a device-owner
# exemption on Android) legitimately differ. What is never acceptable is a claim of a change
# the platform did not report, or an answer naming a different direction than the one asked.
ADAPTER_CONTROL_SCENE = SceneEvidence(
    name="adapter-control",
    rf="adapter-control-ui-observation",
    trace=(
        "idle",
        "started",
        "radio:off-answered",
        "radio:on-answered",
        "closed",
    ),
    kind="adapter-control",
)


def recovery_trace(cycles: int) -> tuple[str, ...]:
    """The trace of an N-cycle same-owner recovery, derived rather than copied.

    A recovery is the same cycle repeated, so one definition states it once: the host ends
    the peer's link, the App announces it, and the next cycle proves the owner is still
    usable. Writing ten cycles out by hand would be ten chances to disagree with the runner.
    """
    if type(cycles) is not int or not 1 <= cycles <= 10:
        raise ValueError("recovery cycles must be an integer in [1, 10]")
    steps = ("scan", "link", "dropped", "announced")
    return (
        ("idle", "started")
        + tuple(f"cycle:{n}:{step}" for n in range(cycles) for step in steps)
        + ("closed",)
    )


# N peer-initiated ends, each followed by another cycle on the **same owner**. What a
# recovery proves is not "it reconnects" — it is that a link end nobody here asked for
# leaves nothing behind: no stale value, no stale command, and the owner stays usable.
RECOVERY_SCENE = SceneEvidence(
    name="recovery",
    rf="recovery-ui-observation",
    trace=recovery_trace(3),
    characteristic_role="read-write",
    kind="recovery",
    cycles=3,
    min_seconds=300,
)

# The same cycle, ten times, on the same owner. The budget is stated because the cycle is
# click-bound on iOS (~9 s per click, six clicks per cycle) and a run that cannot afford ten
# of them must say so before it drives rather than after.
RECOVERY_TEN_SCENE = SceneEvidence(
    name="recovery-ten",
    rf="recovery-ten-ui-observation",
    trace=recovery_trace(10),
    characteristic_role="read-write",
    kind="recovery",
    cycles=10,
    min_seconds=1500,
)
SCENES = {
    scene.name: scene
    for scene in (
        SCAN_SCENE,
        NOTIFICATION_SCENE,
        INDICATION_SCENE,
        IO_SCENE,
        CANCELLATION_SCENE,
        PASSIVE_DISCONNECT_SCENE,
        RECOVERY_SCENE,
        RECOVERY_TEN_SCENE,
        WRITE_REJECT_SCENE,
        CCC_ENABLE_REJECT_SCENE,
        CCC_DISABLE_REJECT_SCENE,
        BACKGROUND_SCENE,
        DISCONNECT_SCENE,
        CONNECT_FAILURE_SCENE,
        LINK_FACTS_SCENE,
        DESCRIPTOR_IO_SCENE,
        DEVICES_QUERY_SCENE,
        AUTO_CONNECT_SCENE,
        DEVICES_KINDS_SCENE,
        ADAPTER_CONTROL_SCENE,
    )
}
# Kept as the scan scene's own names: existing scan producers and their reports use them.
TRACE = list(SCAN_SCENE.trace)

DIRECTORIES = {
    "candidate",
    "candidate.app",
    "platform",
    "driver",
    "fixture",
    "scan",
}
ROOT_FILES = {
    "candidate.apk",
    "request.json",
    "phone.json",
    "prepared-result.json",
    "result.json",
    "manifest.json",
}

# A managed scene bundle is sealed with the same stages on both platforms, but the
# frozen candidate and the platform preparation are genuinely different: Android
# installs one APK and prepares helpers/permissions, iOS installs one App tree and
# builds one WebDriverAgent runner. Each platform therefore has its own exact field
# set instead of one set that would have to tolerate fields the other cannot mean.
COMMON_RESULT_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "device",
    "app_id",
    "status",
    "host_gate_status",
    "rf",
    "scenario_status",
    "session_status",
    "phone_status",
    "fixture_status",
    "fixture_close",
    "primary_error",
    "cleanup_errors",
    "phone_cleanup_errors",
    "cleanup_order",
}
ANDROID_RESULT_FIELDS = COMMON_RESULT_FIELDS | {"apk_sha256"}
IOS_RESULT_FIELDS = COMMON_RESULT_FIELDS | {"app_tree_sha256"}
PLATFORM_RESULT_FIELDS = {"android": ANDROID_RESULT_FIELDS, "ios": IOS_RESULT_FIELDS}
# A run may carry an observation that is not needed to interpret older bundles: evidence
# recorded before the field existed must still replay, so these are allowed rather than
# required, and are shape-checked when present (`_display_observation`).
OPTIONAL_RESULT_FIELDS = {"phone_display"}
# The managed lanes that may produce a scene bundle. A scope names one of them, so an
# unknown platform is a rejected claim rather than a longer string.
MANAGED_PLATFORMS = ("android", "ios")
IOS_NATIVE_DRIVER_KIND = "ios-xctest-in-process"
ANDROID_NATIVE_DRIVER_KIND = "android-uiautomator-in-process"
# The driver kinds a bundle may name. A managed run states which one drove the App instead
# of assuming a remote automation server, because the in-process lanes own none.
DRIVER_KINDS = (
    IOS_NATIVE_DRIVER_KIND,
    ANDROID_NATIVE_DRIVER_KIND,
)
IOS_NATIVE_REQUEST_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "device",
    "app_id",
    "executable",
    "app_tree_sha256",
    "team",
    "identity",
    "driver_kind",
    "xctestrun_sha256",
    "fixture_resource_identity",
    "fixture_port",
    "service_uuid",
    "authorize_install",
    "authorize_fixture_start",
    "timeout",
    "cleanup_seconds",
    "fixture_pid",
    "fixture_pgid",
}
# An in-process instrumentation run has no Appium server, no helper APKs and no HANS
# exemption, so its request states the driver and the exact test APK instead of all of them.
ANDROID_NATIVE_REQUEST_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "device",
    "app_id",
    "activity",
    "apk_sha256",
    "driver_kind",
    "test_apk_sha256",
    "fixture_resource_identity",
    "fixture_port",
    "service_uuid",
    "authorize_install",
    "authorize_fixture_start",
    "authorize_bluetooth_permissions",
    "use_root",
    "timeout",
    "cleanup_seconds",
    "fixture_pid",
    "fixture_pgid",
}
PERMISSION_GRANTS = {
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.BLUETOOTH_CONNECT",
}
LEASE_FIELDS = {
    "schema_version",
    "run_id",
    "resource_identity",
    "observed_resource_identity",
    "status",
    "acquired",
    "fixture_pid",
    "fixture_pgid",
    "error",
}
START_FIELDS = {"run_id", "status", "resource_identity", "error"}
CLOSE_FIELDS = {"run_id", "resource_identity", "status", "receipt", "error"}
CLOSE_RECEIPT_FIELDS = {
    "session_id",
    "resource_identity",
    "boot_nonce",
    "supported_settings",
    "current_settings",
    "proof_method",
    "advertising_inactive",
    "resources_closed",
}
# These are the provider's canonical snapshot names. Do not create a second
# fixture codec here: load_session/verify_receipt remain the source of truth.
CONTRACT_NAME = fixture_session.CONTRACT_NAME
PROFILE_NAME = fixture_session.PROFILE_NAME
REQUEST_NAME = fixture_session.REQUEST_NAME
RECEIPT_NAME = fixture_session.RECEIPT_NAME


class ScanEvidenceError(ValueError):
    """The recorded scan evidence cannot support its claimed scope."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScanEvidenceError(message)


def _path(path: Path) -> Path:
    _require(isinstance(path, Path), "attempt path required")
    _require(".." not in path.parts, "path traversal")
    path = path.absolute()
    evidence_io.no_links(path)
    return path


def _deadline(caller_deadline: float | None = None) -> float:
    now = time.monotonic()
    if caller_deadline is None:
        return now + MAX_SECONDS
    _require(
        type(caller_deadline) in (int, float)
        and not isinstance(caller_deadline, bool)
        and math.isfinite(caller_deadline),
        "finite publication deadline required",
    )
    return min(now + MAX_SECONDS, float(caller_deadline))


def _uuid(value: Any, label: str) -> None:
    try:
        _require(type(value) is str and str(uuid.UUID(value)) == value, label)
    except (ValueError, AttributeError):
        raise ScanEvidenceError(label) from None


def _sha(value: Any, label: str) -> None:
    _require(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        label,
    )


def _finite(value: Any, label: str, maximum: float | None = None) -> None:
    _require(type(value) in (int, float) and not isinstance(value, bool), label)
    _require(math.isfinite(value) and 0 < value, label)
    if maximum is not None:
        _require(value <= maximum, label)


def _strings(value: Any, label: str) -> None:
    _require(
        type(value) is list and all(type(item) is str and bool(item) for item in value),
        label,
    )


def _device(value: Any) -> None:
    _require(
        type(value) is str
        and re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is not None
        and not value.casefold().startswith("emulator-"),
        "explicit Android device required",
    )


def _package(value: Any, label: str = "canonical app_id required") -> None:
    _require(
        type(value) is str
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", value)
        is not None,
        label,
    )


def _ios_bundle(value: Any, label: str = "canonical iOS bundle id required") -> None:
    """Apple's bundle-id charset allows hyphens; Android package ids do not."""
    _require(
        type(value) is str
        and re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value) is not None,
        label,
    )


def _activity(value: Any) -> None:
    _require(
        type(value) is str
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_$]*)+", value)
        is not None,
        "canonical Android activity required",
    )


def _summary(result: dict) -> dict:
    # Cover every result field, not just the display summary. This makes a
    # consistently resealed result mutation fail manifest replay.
    return {
        field: result[field]
        for field in sorted(PLATFORM_RESULT_FIELDS[result["platform"]])
    }


# A frozen candidate is the artifact a device gate installs, so it is inspected once, before
# admission, as a release-shaped payload rather than trusted by hash alone. This is an
# inventory claim only: it says nothing about signing, ELF contents or installed identity.
MAX_APK_BYTES = 512 * 1024 * 1024
MAX_APK_ENTRIES = 20_000
MAX_APK_EXPANDED_BYTES = 1024 * 1024 * 1024
REQUIRED_APK_NATIVE = ("libbleng_uniffi.so", "libBleHubReactNativeBleng.so")


def require_release_apk(path: Path, deadline: float) -> None:
    """Prove the APK carries the JS bundle, a dex and this project's one native image set."""
    with evidence_io.open_stream(path, MAX_APK_BYTES) as raw:
        before = evidence_io.identity(os.fstat(raw.fileno()))
        count = evidence_files.zip_directory_bound(
            raw, max_entries=MAX_APK_ENTRIES, require=_require
        )
        files: dict[str, int] = {}
        names: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(raw) as archive:
                entries = archive.infolist()
                _require(len(entries) == count, "bad APK directory count")
                for entry in entries:
                    evidence_io.check_deadline(deadline)
                    name = evidence_files.safe_name(
                        entry.filename[:-1] if entry.is_dir() else entry.filename,
                        require=_require,
                    )
                    _require(
                        entry.orig_filename == entry.filename and name not in names,
                        "duplicate/altered APK path",
                    )
                    names.add(name)
                    kind = stat.S_IFMT(entry.external_attr >> 16)
                    _require(
                        kind in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                        and not entry.flag_bits & 1,
                        "linked/special/encrypted APK entry",
                    )
                    total += entry.file_size
                    _require(
                        0 <= entry.file_size <= MAX_APK_BYTES
                        and total <= MAX_APK_EXPANDED_BYTES,
                        "APK expanded size exceeds limit",
                    )
                    size = 0
                    with archive.open(entry) as stream:
                        while block := stream.read(evidence_io.CHUNK_BYTES):
                            evidence_io.check_deadline(deadline)
                            size += len(block)
                            _require(
                                size <= entry.file_size and size <= MAX_APK_BYTES,
                                "APK entry exceeds size limit",
                            )
                    _require(
                        size == entry.file_size and (not entry.is_dir() or size == 0),
                        "bad APK entry size",
                    )
                    if not entry.is_dir():
                        files[name] = size
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
            raise ScanEvidenceError(f"invalid APK: {error}") from error
        _require(
            before
            == evidence_io.identity(os.fstat(raw.fileno()))
            == evidence_io.identity(path.lstat()),
            "APK drift during inspection",
        )
    _require(
        files.get("assets/index.android.bundle", 0) > 0, "missing/empty bundled JS"
    )
    _require(
        any(
            size > 0 and re.fullmatch(r"classes(?:[1-9][0-9]*)?\.dex", name)
            for name, size in files.items()
        ),
        "missing/empty dex",
    )
    libraries = {name for name in files if re.fullmatch(r"lib/[^/]+/[^/]+\.so", name)}
    abis = {name.split("/")[1] for name in libraries}
    _require(bool(abis), "missing BLeng native images")
    for abi in abis:
        _require(
            all(files.get(f"lib/{abi}/{name}", 0) > 0 for name in REQUIRED_APK_NATIVE),
            f"missing/empty formal BLeng native image for {abi}",
        )
    _require(
        not any(
            ("blehub" in Path(name).name.lower() or "bleng" in Path(name).name.lower())
            and Path(name).name not in REQUIRED_APK_NATIVE
            for name in libraries
        ),
        "unexpected/legacy BleHub or BLeng native image",
    )
    evidence_io.check_deadline(deadline)


def _snapshot(attempt: Path, deadline: float):
    return evidence_io.snapshot(
        attempt,
        deadline,
        allowed_directories=DIRECTORIES,
        allowed_root_files=ROOT_FILES,
    )


def _documents(attempt: Path, files: list[dict], deadline: float) -> dict[str, dict]:
    return {
        row["path"]: evidence_io.read_document(attempt / row["path"], deadline)
        for row in files
        if row["path"].endswith(".json") and row["path"] != "result.json"
    }


def _display_observation(value: Any) -> None:
    """The screen measurement a run may record; a raw number would be a claim, not evidence."""
    _require(
        type(value) is dict
        and set(value) == {"awake", "luminance", "sample"}
        and type(value["awake"]) is bool
        and type(value["luminance"]) in (int, float)
        and value["luminance"] >= 0
        and value["sample"] == "8x8",
        "invalid display observation",
    )


def _result(
    result: Any, *, prepared: bool = False, scene: SceneEvidence = SCAN_SCENE
) -> None:
    platform = result.get("platform") if type(result) is dict else None
    _require(
        type(result) is dict
        and platform in PLATFORM_RESULT_FIELDS
        and set(result) - OPTIONAL_RESULT_FIELDS == PLATFORM_RESULT_FIELDS[platform],
        "invalid scan result fields",
    )
    if "phone_display" in result:
        _display_observation(result["phone_display"])
    _require(
        type(result["schema_version"]) is int
        and result["schema_version"] == 1
        and result["scope"] == scene.scope(platform)
        and platform in MANAGED_PLATFORMS
        and result["rf"] == scene.rf,
        "invalid scan schema/scope/platform/RF claim",
    )
    _uuid(result["run_id"], "canonical run UUID required")
    _device(result["device"])
    if platform == "ios":
        _ios_bundle(result["app_id"])
        _sha(result["app_tree_sha256"], "canonical App tree hash required")
    else:
        _package(result["app_id"])
        _sha(result["apk_sha256"], "canonical APK hash required")
    for field in ("scenario_status", "session_status"):
        # A prepared document is the producer's pre-finalization view: the inner runner
        # publishes it before it folds cleanup into the session/gate verdict, so only a
        # prepared snapshot may still be pending.
        allowed = (
            ("not_run", "pending", "passed", "failed")
            if prepared
            else ("not_run", "passed", "failed")
        )
        _require(result[field] in allowed, f"invalid {field}")
    _require(
        result["host_gate_status"] in ("pending", "failed")
        and result["status"] in ("passed", "failed")
        and result["phone_status"]
        in ("not_acquired", "passed", "failed", "quarantined")
        and result["fixture_status"] in ("not_acquired", "passed", "failed"),
        "invalid scan stage/status",
    )
    _require(
        result["primary_error"] is None
        or (type(result["primary_error"]) is str and bool(result["primary_error"])),
        "invalid primary error",
    )
    for field in ("cleanup_errors", "phone_cleanup_errors", "cleanup_order"):
        _strings(result[field], f"invalid {field}")
    _close_receipt(result["fixture_close"], run_id=result["run_id"])
    if prepared:
        _require(
            result["host_gate_status"] == "pending",
            "prepared scan gate must remain pending",
        )
        return
    if result["status"] == "passed":
        _require(
            result["scenario_status"]
            == result["session_status"]
            == result["phone_status"]
            == result["fixture_status"]
            == "passed"
            and result["host_gate_status"] == "pending"
            and result["primary_error"] is None
            and not result["cleanup_errors"]
            and not result["phone_cleanup_errors"],
            "inconsistent scan success",
        )
    else:
        _require(
            result["primary_error"] is not None
            or result["cleanup_errors"]
            or result["phone_cleanup_errors"],
            "failed scan must preserve a cause",
        )


def _fixture_selection_facts(request: dict) -> None:
    """Canonical fixture identity/port/service facts, shared by both platforms."""
    _require(
        type(request["fixture_resource_identity"]) is str
        and bool(request["fixture_resource_identity"]),
        "fixture resource identity required",
    )
    try:
        kind, serial = request["fixture_resource_identity"].split(":", 1)
        from tests.peripherals.nrf52840.resource_lease import canonical_identity

        _require(
            canonical_identity(kind, serial) == request["fixture_resource_identity"],
            "noncanonical fixture resource identity",
        )
    except (ValueError, RuntimeError, AttributeError):
        raise ScanEvidenceError("invalid fixture resource identity") from None
    _require(
        type(request["fixture_port"]) is str and bool(request["fixture_port"]),
        "explicit fixture port required",
    )
    _require(
        ".." not in Path(request["fixture_port"]).parts, "fixture port path traversal"
    )
    _require(
        type(request["service_uuid"]) is str
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            request["service_uuid"].casefold(),
        )
        is not None,
        "canonical fixture service UUID required",
    )


def _request(request: Any, result: dict, scene: SceneEvidence = SCAN_SCENE) -> None:
    """Accept only the in-process request shape of the platform that produced the bundle."""
    fields = (
        IOS_NATIVE_REQUEST_FIELDS
        if result["platform"] == "ios"
        else ANDROID_NATIVE_REQUEST_FIELDS
    )
    _require(
        type(request) is dict and set(request) == fields,
        "invalid scene request fields",
    )
    if result["platform"] == "ios":
        _ios_native_request(request, result, scene)
    else:
        _android_native_request(request, result, scene)


def _android_native_request(
    request: dict, result: dict, scene: SceneEvidence = SCAN_SCENE
) -> None:
    """The in-process lane's request: no server, no helpers, one test APK."""
    _require(
        type(request["schema_version"]) is int
        and request["schema_version"] == 1
        and request["scope"] == scene.scope("android")
        and request["run_id"] == result["run_id"]
        and request["device"] == result["device"]
        and request["app_id"] == result["app_id"]
        and request["apk_sha256"] == result["apk_sha256"],
        "Android in-process request/result identity conflict",
    )
    _require(
        request["driver_kind"] == ANDROID_NATIVE_DRIVER_KIND,
        "the Android in-process request must name its driver",
    )
    _activity(request["activity"])
    _device(request["device"])
    _package(request["app_id"])
    _sha(request["apk_sha256"], "invalid request APK hash")
    _sha(request["test_apk_sha256"], "invalid request test APK hash")
    _fixture_selection_facts(request)
    for field in (
        "authorize_install",
        "authorize_fixture_start",
        "authorize_bluetooth_permissions",
    ):
        _require(request[field] is True, f"{field} must be explicitly true")
    _require(type(request["use_root"]) is bool, "use_root must be bool")
    from .bleng_android_scan import MAX_TOTAL_SECONDS

    _finite(request["timeout"], "invalid scan timeout", MAX_TOTAL_SECONDS)
    _finite(request["cleanup_seconds"], "invalid scan cleanup budget", 300)
    for field in ("fixture_pid", "fixture_pgid"):
        _require(type(request[field]) is int and request[field] > 0, f"invalid {field}")


def _ios_native_request(
    request: dict, result: dict, scene: SceneEvidence = SCAN_SCENE
) -> None:
    """The in-process lane's request: no ports, no WDA build, one test bundle."""
    _require(
        type(request["schema_version"]) is int
        and request["schema_version"] == 1
        and request["scope"] == scene.scope("ios")
        and request["run_id"] == result["run_id"]
        and request["device"] == result["device"]
        and request["app_id"] == result["app_id"]
        and request["app_tree_sha256"] == result["app_tree_sha256"],
        "iOS scan request/result identity conflict",
    )
    _require(
        request["driver_kind"] == IOS_NATIVE_DRIVER_KIND,
        "the native iOS request must name the in-process driver",
    )
    _sha(request["xctestrun_sha256"], "invalid request xctestrun hash")
    _device(request["device"])
    _ios_bundle(request["app_id"])
    _sha(request["app_tree_sha256"], "invalid request App tree hash")
    _require(
        type(request["executable"]) is str
        and re.fullmatch(r"[A-Za-z0-9_-]+", request["executable"]) is not None,
        "invalid request App executable",
    )
    _require(
        type(request["team"]) is str
        and re.fullmatch(r"[A-Z0-9]{10}", request["team"]) is not None,
        "invalid request signing team",
    )
    _require(
        type(request["identity"]) is str
        and request["identity"].startswith("Apple Development:"),
        "invalid request signing identity",
    )
    _fixture_selection_facts(request)
    for field in ("authorize_install", "authorize_fixture_start"):
        _require(request[field] is True, f"{field} must be explicitly true")
    from .bleng_ios_scan import MAX_CLEANUP_SECONDS, MAX_TOTAL_SECONDS

    _finite(request["timeout"], "invalid scan timeout", MAX_TOTAL_SECONDS)
    _finite(
        request["cleanup_seconds"],
        "invalid scan cleanup budget",
        MAX_CLEANUP_SECONDS,
    )
    for field in ("fixture_pid", "fixture_pgid"):
        _require(type(request[field]) is int and request[field] > 0, f"invalid {field}")


def _close_receipt(
    value: Any, *, run_id: str, resource_identity: str | None = None
) -> None:
    if value is None:
        return
    _require(
        type(value) is dict and set(value) == CLOSE_RECEIPT_FIELDS,
        "invalid passive fixture close receipt",
    )
    _require(value["session_id"] == run_id, "fixture close session conflict")
    _require(
        type(value["resource_identity"]) is str and bool(value["resource_identity"]),
        "invalid fixture close resource",
    )
    if resource_identity is not None:
        _require(
            value["resource_identity"] == resource_identity,
            "fixture close resource identity conflict",
        )
    _require(
        type(value["boot_nonce"]) is str
        and re.fullmatch(r"[0-9a-f]{32}", value["boot_nonce"]) is not None,
        "invalid fixture boot nonce",
    )
    for field in ("supported_settings", "current_settings"):
        _require(
            type(value[field]) is int
            and not isinstance(value[field], bool)
            and value[field] >= 0,
            "invalid fixture settings readback",
        )
    _require(
        value["supported_settings"] & (1 << 10)
        and not value["current_settings"] & (1 << 10),
        "fixture advertising-bit readback is not proven inactive",
    )
    _require(
        value["proof_method"] == "btp-gap-controller-info-advertising-bit-clear",
        "invalid passive close proof method",
    )
    _require(
        value["advertising_inactive"] is True and value["resources_closed"] is True,
        "passive close is not verified",
    )


def _lease_facts(
    doc: dict,
    result: dict,
    resource: str,
    *,
    fixture_pid: int,
    fixture_pgid: int,
) -> None:
    _require(
        type(doc) is dict and set(doc) == LEASE_FIELDS, "invalid fixture lease receipt"
    )
    _require(
        doc["schema_version"] == 1
        and doc["run_id"] == result["run_id"]
        and doc["resource_identity"] == resource,
        "fixture lease identity conflict",
    )
    _require(
        doc["status"] in ("not_acquired", "released", "failed"),
        "invalid fixture lease status",
    )
    _require(type(doc["acquired"]) is bool, "invalid fixture lease acquired flag")
    observed = doc["observed_resource_identity"]
    if observed is None:
        _require(not doc["acquired"], "acquired fixture lease lacks observed identity")
    else:
        _require(
            type(observed) is str and bool(observed),
            "invalid observed fixture identity",
        )
        try:
            from tests.peripherals.nrf52840.resource_lease import canonical_identity

            _require(
                canonical_identity(*observed.split(":", 1)) == observed
                if observed.count(":") == 1
                else False,
                "noncanonical observed fixture identity",
            )
        except (ValueError, RuntimeError, AttributeError):
            raise ScanEvidenceError("invalid observed fixture identity") from None
    _require(
        type(doc["fixture_pid"]) is int
        and doc["fixture_pid"] > 0
        and doc["fixture_pid"] == fixture_pid
        and type(doc["fixture_pgid"]) is int
        and doc["fixture_pgid"] > 0
        and doc["fixture_pgid"] == fixture_pgid,
        "fixture lease process identity conflict",
    )
    if result["status"] == "passed" or result["fixture_status"] == "passed":
        _require(
            observed == resource,
            "passed fixture lease observed identity differs from request",
        )
    _require(
        doc["error"] is None or (type(doc["error"]) is str and bool(doc["error"])),
        "invalid fixture lease error",
    )
    if doc["status"] == "released":
        _require(
            doc["acquired"] is True and doc["error"] is None,
            "released fixture lease is incomplete",
        )
    if doc["status"] == "failed":
        _require(doc["error"] is not None, "failed fixture lease lacks error")


def _start_facts(doc: dict, result: dict, resource: str) -> None:
    _require(
        type(doc) is dict and set(doc) == START_FIELDS, "invalid fixture start receipt"
    )
    _require(
        doc["run_id"] == result["run_id"] and doc["resource_identity"] == resource,
        "fixture start identity conflict",
    )
    _require(doc["status"] in ("passed", "failed"), "invalid fixture start status")
    _require(
        doc["error"] is None or (type(doc["error"]) is str and bool(doc["error"])),
        "invalid fixture start error",
    )
    if doc["status"] == "passed":
        _require(doc["error"] is None, "passed fixture start has an error")


def _close_facts(doc: dict, result: dict, resource: str) -> None:
    _require(
        type(doc) is dict and set(doc) == CLOSE_FIELDS, "invalid fixture close receipt"
    )
    _require(
        doc["run_id"] == result["run_id"] and doc["resource_identity"] == resource,
        "fixture close identity conflict",
    )
    _require(doc["status"] in ("passed", "failed"), "invalid fixture close status")
    _require(
        doc["error"] is None or (type(doc["error"]) is str and bool(doc["error"])),
        "invalid fixture close error",
    )
    if doc["status"] == "passed":
        _require(
            doc["error"] is None and type(doc["receipt"]) is dict,
            "passed fixture close lacks receipt",
        )
        _close_receipt(
            doc["receipt"],
            run_id=result["run_id"],
            resource_identity=resource,
        )
    else:
        _require(
            doc["receipt"] is None, "failed fixture close cannot contain a receipt"
        )


def _fixture_facts(
    attempt: Path, docs: dict[str, dict], result: dict, request: dict | None
) -> None:
    root = attempt / "fixture"
    names = [
        f"fixture/{name}"
        for name in (CONTRACT_NAME, PROFILE_NAME, REQUEST_NAME, RECEIPT_NAME)
    ]
    present = [name in docs for name in names]
    lease = docs.get("fixture/lease.json")
    start = docs.get("fixture/start.json")
    close = docs.get("fixture/close.json")
    any_evidence = any(present) or any(
        item is not None for item in (lease, start, close)
    )
    if not any_evidence:
        _require(
            result["status"] != "passed"
            and result["fixture_status"] in ("not_acquired", "failed"),
            "missing fixture snapshot",
        )
        return

    fixture_request: dict | None = None
    profile = plan = receipt = None
    try:
        if present[2]:
            fixture_request = fixture_session._json(
                fixture_session._read_bytes(root / REQUEST_NAME)
            )
            _require(
                set(fixture_request)
                == {
                    "schema_version",
                    "session_id",
                    "contract_sha256",
                    "profile_sha256",
                    "service_uuid",
                    "port",
                    "resource_identity",
                },
                "invalid partial fixture request",
            )
        if present[0]:
            fixture_session.load_contract(root / CONTRACT_NAME)
        if present[1]:
            profile = fixture_session.load_profile(root / PROFILE_NAME)
        if present[0] and present[1]:
            assert profile is not None
            service_uuid = (
                request["service_uuid"] if request is not None else profile.service_uuid
            )
            fixture_session.validate_fixture_inputs(
                root / CONTRACT_NAME,
                root / PROFILE_NAME,
                service_uuid=service_uuid,
            )
        if all(present[:3]):
            plan = fixture_session.load_session(root / REQUEST_NAME)
        if present[3]:
            receipt_document = fixture_session._json(
                fixture_session._read_bytes(root / RECEIPT_NAME)
            )
            if plan is not None:
                receipt = fixture_session.verify_receipt_contents(
                    plan,
                    receipt_document,
                    fixture_identity=(
                        receipt_document["fixture_pid"],
                        receipt_document["fixture_pgid"],
                    ),
                )
            else:
                _require(
                    type(receipt_document.get("installed_identity")) is dict,
                    "partial fixture receipt lacks installed identity",
                )
                fixture_session.parse_attestation(
                    receipt_document["installed_identity"]
                )
    except Exception as error:  # provider code supplies canonical parsing/validation
        raise ScanEvidenceError(f"invalid offline fixture evidence: {error}") from error

    resource = (
        request.get("fixture_resource_identity")
        if request is not None
        else fixture_request.get("resource_identity")
        if fixture_request is not None
        else None
    )
    for document in (lease, start, close):
        if document is None:
            continue
        observed = document.get("resource_identity")
        if resource is None:
            resource = observed
        _require(
            type(observed) is str and bool(observed) and observed == resource,
            "fixture lifecycle resource identity conflict",
        )
    _require(
        resource is not None and type(resource) is str and bool(resource),
        "fixture lifecycle resource identity is missing",
    )
    assert isinstance(resource, str)

    expected_pid = request.get("fixture_pid") if request is not None else None
    expected_pgid = request.get("fixture_pgid") if request is not None else None
    if lease is not None and expected_pid is None:
        expected_pid, expected_pgid = (
            lease.get("fixture_pid"),
            lease.get("fixture_pgid"),
        )
    if lease is not None:
        _require(
            type(expected_pid) is int
            and expected_pid > 0
            and type(expected_pgid) is int
            and expected_pgid > 0,
            "fixture lease process identity is missing",
        )
        assert type(expected_pid) is int and type(expected_pgid) is int
        _lease_facts(
            lease,
            result,
            resource,
            fixture_pid=expected_pid,
            fixture_pgid=expected_pgid,
        )
    if start is not None:
        _start_facts(start, result, resource)
    if close is not None:
        _close_facts(close, result, resource)

    complete = all(present)
    if receipt is not None and request is not None:
        _require(
            receipt["fixture_pid"] == request["fixture_pid"]
            and receipt["fixture_pgid"] == request["fixture_pgid"],
            "fixture request/receipt process identity conflict",
        )
    if plan is not None and request is not None:
        _require(
            plan.session_id == result["run_id"]
            and plan.resource_identity == request["fixture_resource_identity"]
            and plan.port == request["fixture_port"]
            and plan.service_uuid == request["service_uuid"].casefold(),
            "fixture request selection drift",
        )
    if not complete:
        _require(
            result["status"] != "passed"
            and result["fixture_status"] != "passed"
            and (start is None or start["status"] != "passed"),
            "incomplete fixture snapshot cannot support success",
        )
    if result["fixture_status"] == "passed":
        _require(
            complete
            and plan is not None
            and receipt is not None
            and lease is not None
            and start is not None
            and close is not None,
            "passed fixture lacks complete proof",
        )
        assert lease is not None and start is not None and close is not None
        _require(
            lease["status"] == "released"
            and start["status"] == "passed"
            and close["status"] == "passed"
            and result["fixture_close"] == close["receipt"],
            "passed fixture lifecycle proof is incomplete",
        )

    for name in (REQUEST_NAME, RECEIPT_NAME):
        outer_path = attempt / "fixture" / name
        inner_name = f"scan/{name}"
        if inner_name in docs:
            _require(outer_path.is_file(), f"inner fixture {name} lacks outer snapshot")
            _require(
                (attempt / inner_name).read_bytes() == outer_path.read_bytes(),
                f"inner fixture {name} differs from outer snapshot",
            )


def _candidate_facts(
    attempt: Path,
    docs: dict[str, dict],
    files: list[dict],
    result: dict,
    deadline: float,
) -> None:
    apk = next((row for row in files if row["path"] == "candidate.apk"), None)
    if apk is not None:
        _require(apk["sha256"] == result["apk_sha256"], "candidate APK hash mismatch")
        require_release_apk(attempt / "candidate.apk", deadline)
    if result["status"] != "passed":
        return
    _require(apk is not None, "successful scan lacks frozen APK")
    bound = docs.get("candidate/bound-identity.json")
    final = docs.get("candidate/finalization.json")
    installs = sorted(
        name for name in docs if re.fullmatch(r"candidate/[0-9]{3}-install\.json", name)
    )
    _require(
        bound is not None and final is not None and len(installs) == 1,
        "incomplete candidate success evidence",
    )
    assert bound is not None and final is not None
    _require(
        bound.get("serial") == result["device"]
        and bound.get("package") == result["app_id"]
        and bound.get("apk_sha256") == result["apk_sha256"],
        "candidate identity conflict",
    )
    _require(
        all(
            type(bound.get(field)) is int and bound[field] > 0
            for field in ("pid", "uid", "start_ticks")
        )
        and type(bound.get("base_apk_path")) is str
        and bound["base_apk_path"].startswith("/data/app/"),
        "invalid candidate OS identity",
    )
    release = final.get("released_retained_app")
    # Two shapes are legitimate, and each is checked for what it means:
    #   retain     — the App is still running as the evidence a human inspects;
    #   force-stop — the caller asked for a scene sequence, so the exact installation
    #                this run launched was stopped. `status=passed` means absence was
    #                re-verified after that stop; an absent flag stays legitimate for
    #                retained bundles replayed from before the option existed.
    _require(
        final.get("run_id") == result["run_id"]
        and final.get("status") == "passed"
        and final.get("identity") == bound
        and (
            (final.get("action") == "retain" and release is not True)
            or (final.get("action") == "force-stop" and release is True)
        ),
        "candidate finalization is not a retained App or an authorized release",
    )
    install = docs[installs[0]]
    log_name = installs[0][:-5] + ".log"
    log = next((row for row in files if row["path"] == log_name), None)
    _require(log is not None, "candidate install log is missing")
    assert log is not None
    _require(
        install.get("run_id") == result["run_id"]
        and install.get("serial") == result["device"]
        and install.get("apk_sha256") == result["apk_sha256"]
        and install.get("status") == "passed"
        and install.get("grant") is False
        and install.get("stdout_sha256") == log["sha256"],
        "candidate install/log conflict",
    )
    _require(
        not any(doc.get("action", "").casefold() == "delete" for doc in (final,)),
        "unknown candidate DELETE cannot support success",
    )


def _ios_core_device_receipt(value: Any, command: str) -> tuple[dict, dict]:
    """Split one CoreDevice JSON receipt and state which command it must prove."""
    _require(
        type(value) is dict
        and type(value.get("info")) is dict
        and type(value.get("result")) is dict
        and value["info"].get("commandType") == command,
        f"invalid CoreDevice {command} receipt",
    )
    return value["info"], value["result"]


def _ios_receipt_for_device(info: dict, device: str, command: str) -> None:
    """Tie one receipt to the run's device by the command it actually recorded.

    CoreDevice answers with its own ``deviceIdentifier`` (a UUID), while a run names the
    device by UDID, so the two cannot be compared to each other. What the receipt does
    record is the exact argv it ran, and `--device <udid>` is part of it; the receipts of
    one run are also cross-checked against each other below, so this stays a proof that
    both the install and the launch happened to this run's device.
    """
    arguments = info.get("arguments")
    _require(
        type(arguments) is list
        and arguments.count("--device") == 1
        and arguments[arguments.index("--device") + 1 :][:1] == [device],
        f"candidate {command} receipt names another device",
    )


def _ios_candidate_facts(
    attempt: Path, docs: dict[str, dict], result: dict, deadline: float
) -> None:
    """A frozen App tree plus the official install/launch receipts and final identity."""
    frozen = attempt / "candidate.app"
    if result["status"] != "passed":
        return
    _require(
        frozen.is_dir() and not frozen.is_symlink(),
        "successful scan lacks the frozen App tree",
    )
    from tools.apple.app_artifact import fingerprint

    _require(
        fingerprint(
            frozen, require_current=lambda: evidence_io.check_deadline(deadline)
        )
        == result["app_tree_sha256"],
        "frozen App tree hash mismatch",
    )
    install = docs.get("candidate/02-install.json")
    launch = docs.get("candidate/04-launch.json")
    final = docs.get("candidate/finalization/result.json")
    _require(
        install is not None and launch is not None and final is not None,
        "incomplete candidate success evidence",
    )
    assert install is not None and launch is not None and final is not None
    install_info, install_result = _ios_core_device_receipt(
        install, "devicectl.device.install.app"
    )
    launch_info, launch_result = _ios_core_device_receipt(
        launch, "devicectl.device.process.launch"
    )
    _ios_receipt_for_device(install_info, result["device"], "install")
    _ios_receipt_for_device(launch_info, result["device"], "launch")
    core = install_result.get("deviceIdentifier")
    _require(
        isinstance(core, str)
        and bool(core)
        and core == launch_result.get("deviceIdentifier"),
        "candidate install/launch disagree on the device",
    )
    rows = install_result.get("installedApplications")
    _require(
        type(rows) is list
        and len(rows) == 1
        and type(rows[0]) is dict
        and rows[0].get("bundleID") == result["app_id"],
        "candidate install receipt is not this App",
    )
    arguments = launch_info.get("arguments")
    _require(
        type(arguments) is list and arguments.count(result["app_id"]) == 1,
        "candidate launch receipt is not this App",
    )
    identity = final.get("identity")
    _require(
        final.get("scope") == "formal-ios-candidate-finalization"
        and final.get("run_id") == result["run_id"]
        and final.get("status") == "passed"
        and type(identity) is dict
        and identity.get("run_id") == result["run_id"]
        and identity.get("bundle_id") == result["app_id"]
        and identity.get("device") == result["device"]
        and type(identity.get("pid")) is int
        and identity["pid"] > 0,
        "candidate finalization is not this run's installed App",
    )
    release = final.get("released_retained_app")
    _require(
        (final.get("action") == "retain" and release is False)
        or (final.get("action") == "terminate" and release is True),
        "candidate finalization is not a retained App or an authorized release",
    )
    _require(
        any(
            name.startswith("candidate/ios-identity-")
            and name.endswith("/01-processes-before.json")
            for name in docs
        ),
        "candidate lacks a fresh native identity challenge",
    )


def _automation_facts(docs: dict[str, dict], result: dict, inner: dict | None) -> None:
    """What drove the App, stated by kind rather than assumed to be a server."""
    if result["status"] != "passed":
        return
    driver = inner.get("driver") if isinstance(inner, dict) else None
    _require(isinstance(driver, dict), "scene result names no driver")
    assert isinstance(driver, dict)
    kind = driver.get("kind")
    if result["platform"] == "ios":
        _require(
            kind == IOS_NATIVE_DRIVER_KIND,
            "the iOS scene was not driven in process",
        )
        _ios_native_driver_facts(docs, result, inner or {}, driver)
        return
    _require(
        kind == ANDROID_NATIVE_DRIVER_KIND,
        "the Android scene was not driven in process",
    )
    _android_native_driver_facts(docs, result, inner or {}, driver)


def _ios_native_driver_facts(
    docs: dict[str, dict], result: dict, inner: dict, driver: dict
) -> None:
    """The in-process XCTest lane: one phase list, nothing resident.

    There is deliberately no WDA/server/helper claim here, because this lane owns none of
    them. What it must prove is narrower and stronger: the record states the exact
    xctestrun, the phases that ran, a zero exit status and a passed receipt for each, and
    every receipt it points at is the same artifact with the same artifact hash. A scene
    with more than one phase exists because the host had to act between them (a
    notification cannot be delivered before the subscription is enabled), so the record
    has to describe the whole sequence, not just the last run.
    """
    record = docs.get("driver/record.json")
    _require(
        type(record) is dict,
        "incomplete managed iOS in-process driver evidence",
    )
    assert isinstance(record, dict)
    _require(
        record.get("run_id") == result["run_id"]
        and record.get("kind") == IOS_NATIVE_DRIVER_KIND
        and record.get("session_id") == inner.get("driver_session_id")
        and driver.get("session_id") == inner.get("driver_session_id")
        and record.get("device") == result["device"]
        and record.get("app_id") == result["app_id"],
        "invalid in-process driver identity",
    )
    _require(
        record.get("status") == "passed"
        and record.get("primary_error") is None
        and type(record.get("xctestrun_sha256")) is str
        and re.fullmatch(r"[0-9a-f]{64}", record["xctestrun_sha256"]) is not None
        and driver.get("xctestrun_sha256") == record["xctestrun_sha256"]
        and record.get("plan") == driver.get("plan")
        and type(record.get("steps")) is int
        and record["steps"] == driver.get("steps")
        and record["steps"] > 0,
        "invalid in-process driver record",
    )
    # The scene name the outer producer declared must be the plan that actually ran.
    _require(
        driver.get("plan") is not None
        and result["scope"].endswith("-" + str(driver["plan"])),
        "in-process driver plan conflicts with the scene scope",
    )
    declared = driver.get("phases")
    phases = record.get("phases")
    _require(
        type(phases) is list
        and phases
        and type(declared) is list
        and [phase.get("name") for phase in phases if isinstance(phase, dict)]
        == list(declared),
        "in-process driver phases conflict with the handoff",
    )
    total = 0
    for phase in phases:
        _require(type(phase) is dict, "invalid in-process phase record")
        assert isinstance(phase, dict)
        _require(
            phase.get("status") == "passed"
            and phase.get("xcodebuild_returncode") == 0
            and type(phase.get("steps")) is int
            and phase["steps"] > 0,
            "an in-process phase did not pass",
        )
        relative = phase.get("result")
        _require(
            type(relative) is str
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts,
            "invalid in-process phase result path",
        )
        scene_result = docs.get("driver/" + str(relative))
        _require(
            type(scene_result) is dict
            and scene_result.get("status") == "passed"
            and scene_result.get("scope") == "formal-ios-scene-driver"
            and scene_result.get("run_id") == result["run_id"]
            and scene_result.get("device") == result["device"]
            and scene_result.get("xctestrun_sha256") == record["xctestrun_sha256"]
            and type(scene_result.get("plan")) is str
            and scene_result["plan"].startswith(str(driver.get("plan")))
            and type(scene_result.get("attachments")) is list,
            "an in-process phase receipt conflicts with the driver record",
        )
        total += int(phase["steps"])
    _require(
        total == record["steps"],
        "in-process phase steps do not sum to the recorded total",
    )


def _android_native_driver_facts(
    docs: dict[str, dict], result: dict, inner: dict, driver: dict
) -> None:
    """The in-process instrumentation lane: one phase list, nothing resident.

    Like the iOS lane it makes no server/helper claim, because it owns neither. The record
    must instead name the exact test APK that ran, the phases in order, a zero
    instrumentation exit and a passed receipt for each phase, all against the same test APK
    hash the handoff declared.
    """
    record = docs.get("driver/record.json")
    _require(
        type(record) is dict,
        "incomplete managed Android in-process driver evidence",
    )
    assert isinstance(record, dict)
    _require(
        record.get("run_id") == result["run_id"]
        and record.get("kind") == ANDROID_NATIVE_DRIVER_KIND
        and record.get("session_id") == inner.get("driver_session_id")
        and driver.get("session_id") == inner.get("driver_session_id")
        and record.get("device") == result["device"]
        and record.get("app_id") == result["app_id"],
        "invalid in-process driver identity",
    )
    _require(
        record.get("status") == "passed"
        and record.get("primary_error") is None
        and type(record.get("test_apk_sha256")) is str
        and re.fullmatch(r"[0-9a-f]{64}", record["test_apk_sha256"]) is not None
        and driver.get("test_apk_sha256") == record["test_apk_sha256"]
        and record.get("plan") == driver.get("plan")
        and type(record.get("steps")) is int
        and record["steps"] == driver.get("steps")
        and record["steps"] > 0,
        "invalid in-process driver record",
    )
    _require(
        driver.get("plan") is not None
        and result["scope"].endswith("-" + str(driver["plan"])),
        "in-process driver plan conflicts with the scene scope",
    )
    declared = driver.get("phases")
    phases = record.get("phases")
    _require(
        type(phases) is list
        and phases
        and type(declared) is list
        and [phase.get("name") for phase in phases if isinstance(phase, dict)]
        == list(declared),
        "in-process driver phases conflict with the handoff",
    )
    total = 0
    for phase in phases:
        _require(type(phase) is dict, "invalid in-process phase record")
        assert isinstance(phase, dict)
        _require(
            phase.get("status") == "passed"
            and phase.get("instrumentation_returncode") == 0
            and type(phase.get("steps")) is int
            and phase["steps"] > 0,
            "an in-process phase did not pass",
        )
        relative = phase.get("result")
        _require(
            type(relative) is str
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts,
            "invalid in-process phase result path",
        )
        scene_result = docs.get("driver/" + str(relative))
        _require(
            type(scene_result) is dict
            and scene_result.get("status") == "passed"
            and scene_result.get("scope") == "formal-android-scene-driver"
            and scene_result.get("run_id") == result["run_id"]
            and scene_result.get("device") == result["device"]
            and scene_result.get("test_apk_sha256") == record["test_apk_sha256"],
            "an in-process phase receipt conflicts with the driver record",
        )
        total += int(phase["steps"])
    _require(
        total == record["steps"],
        "in-process phase steps do not sum to the recorded total",
    )


def _phone_facts(
    attempt: Path, docs: dict[str, dict], files: list[dict], result: dict
) -> None:
    phone = docs.get("phone.json")
    if phone is None:
        _require(
            result["status"] != "passed"
            and result["phone_status"] in ("not_acquired", "failed", "quarantined"),
            "missing acquired phone journal",
        )
        return
    if result["platform"] == "ios":
        _require(
            phone.get("platform") == "ios"
            and phone.get("run_id") == result["run_id"]
            and phone.get("device") == result["device"]
            and phone.get("scope") == SCAN_OBSERVATION_SCOPE,
            "iOS phone journal identity/scope conflict",
        )
        _require(
            phone.get("status") in ("held", "released", "quarantined")
            and (phone.get("status") == "quarantined" or phone.get("error") is None),
            "invalid iOS phone journal status",
        )
        if result["status"] == "passed":
            _require(
                phone.get("status") == "released"
                and phone.get("primary_error") is None
                and phone.get("cleanup_errors") == result["phone_cleanup_errors"],
                "iOS phone journal cannot support scan success",
            )
    else:
        _require(
            phone.get("run_id") == result["run_id"]
            and phone.get("status") == result["phone_status"],
            "phone journal identity/status conflict",
        )
        if result["status"] == "passed":
            _require(
                phone.get("actions") == []
                and phone.get("primary_error") is None
                and phone.get("cleanup_errors") == result["phone_cleanup_errors"]
                and phone.get("initial_state") is None
                and phone.get("final_state") is None,
                "phone journal cannot support scan success",
            )
    inner = docs.get("scan/result.json")
    if inner is not None and type(inner.get("phone_journal")) is dict:
        journal = inner["phone_journal"]
        _require(
            type(journal.get("path")) is str
            and Path(journal["path"]).is_absolute()
            and ".." not in Path(journal["path"]).parts
            and journal["path"].endswith("/phone.json"),
            "invalid recorded phone journal path",
        )
        row = next((row for row in files if row["path"] == "phone.json"), None)
        _require(
            row is not None
            and journal.get("sha256") == row["sha256"]
            and journal.get("status") == phone.get("status"),
            "phone journal hash conflict",
        )


def _inner_facts(
    attempt: Path,
    docs: dict[str, dict],
    files: list[dict],
    result: dict,
    request: dict | None,
    scene: SceneEvidence,
) -> None:
    inner = docs.get("scan/result.json")
    prepared = docs.get("scan/prepared-result.json")
    if inner is None:
        _require(result["status"] != "passed", "successful scan lacks inner result")
        return
    _require(
        type(inner) is dict
        and inner.get("schema_version") == 1
        and inner.get("scope") == scene.inner_scope
        and inner.get("run_id") == result["run_id"]
        and inner.get("platform") == result["platform"]
        and inner.get("device") == result["device"]
        and inner.get("app_id") == result["app_id"],
        "inner scan identity conflict",
    )
    _require(
        inner.get("host_gate_status") in ("pending", "failed")
        and inner.get("scenario_status") in ("not_run", "passed", "failed")
        and inner.get("session_status") in ("pending", "not_run", "passed", "failed"),
        "invalid inner scan statuses",
    )
    observations = inner.get("observations")
    _require(
        type(observations) is list and all(type(item) is str for item in observations),
        "invalid inner scan observations",
    )
    if result["status"] == "passed":
        _require(
            inner.get("scenario_status") == "passed"
            and inner.get("session_status") == "passed"
            and inner.get("host_gate_status") == "pending"
            and observations == list(scene.trace),
            "inner scan trace/status cannot support success",
        )
        _require(
            inner.get("fixture_close") == result["fixture_close"],
            "inner/outer fixture close conflict",
        )
        # Both platforms run in process, so the session the inner result names is the one
        # the driver record published — never an automation server's.
        record = docs.get("driver/record.json")
        _require(
            type(record) is dict
            and inner.get("driver_session_id") == record.get("session_id"),
            "inner driver session is not the recorded one",
        )
    fixture = inner.get("fixture")
    if fixture is not None:
        _require(type(fixture) is dict, "invalid inner fixture identity")
        assert isinstance(fixture, dict)
        if request is not None:
            _require(
                fixture.get("resource_identity")
                == request["fixture_resource_identity"],
                "inner fixture identity conflict",
            )
        if result["status"] == "passed":
            canonical_request = docs.get(f"fixture/{REQUEST_NAME}")
            _require(
                type(canonical_request) is dict
                and set(fixture)
                == {"resource_identity", "contract_sha256", "profile_sha256"}
                and fixture.get("contract_sha256")
                == canonical_request.get("contract_sha256")
                and fixture.get("profile_sha256")
                == canonical_request.get("profile_sha256"),
                "inner fixture contract/profile identity conflict",
            )
    inputs = inner.get("fixture_input_sha256")
    if result["status"] == "passed":
        _require(inputs is not None, "inner fixture input hashes are missing")
    if inputs is not None:
        _require(
            type(inputs) is dict and set(inputs) == {REQUEST_NAME, RECEIPT_NAME},
            "invalid inner fixture input hashes",
        )
        for name in (REQUEST_NAME, RECEIPT_NAME):
            row = next((row for row in files if row["path"] == f"scan/{name}"), None)
            _require(
                row is not None and inputs[name] == row["sha256"],
                "inner fixture input hash conflict",
            )
    if prepared is not None:
        _require(
            prepared.get("schema_version") == 1
            and prepared.get("scope") == inner.get("scope")
            and prepared.get("run_id") == result["run_id"]
            and prepared.get("device") == result["device"]
            and prepared.get("app_id") == result["app_id"],
            "prepared inner result identity conflict",
        )
        if prepared.get("primary_error") is not None:
            _require(
                type(prepared["primary_error"]) is str
                and type(inner.get("primary_error")) is str
                and prepared["primary_error"] in inner["primary_error"],
                "prepared inner primary error was lost",
            )
        for field in ("cleanup_errors", "phone_cleanup_errors"):
            if type(prepared.get(field)) is list:
                _require(
                    type(inner.get(field)) is list
                    and inner[field][: len(prepared[field])] == prepared[field],
                    "prepared inner cleanup facts were lost",
                )


def _prepared_facts(
    prepared: dict | None, result: dict, scene: SceneEvidence = SCAN_SCENE
) -> None:
    if prepared is None:
        return
    _result(prepared, prepared=True, scene=scene)
    digest = "apk_sha256" if result["platform"] == "android" else "app_tree_sha256"
    for field in (
        "schema_version",
        "scope",
        "run_id",
        "platform",
        "device",
        "app_id",
        digest,
        "rf",
        "scenario_status",
        "fixture_status",
        "fixture_close",
    ):
        _require(prepared[field] == result[field], f"prepared-result drift: {field}")
    _require(
        result["cleanup_order"][: len(prepared["cleanup_order"])]
        == prepared["cleanup_order"]
        and result["cleanup_errors"][: len(prepared["cleanup_errors"])]
        == prepared["cleanup_errors"]
        and result["phone_cleanup_errors"][: len(prepared["phone_cleanup_errors"])]
        == prepared["phone_cleanup_errors"],
        "prepared scan cleanup facts were lost",
    )
    if prepared["primary_error"] is not None:
        _require(
            type(result["primary_error"]) is str
            and prepared["primary_error"] in result["primary_error"],
            "prepared scan primary error was lost",
        )


def _permission_scope_facts(
    docs: dict[str, dict], result: dict, request: dict | None
) -> None:
    """Validate formal Android Bluetooth permission evidence independently.

    Permission preparation/restoration is an OS cleanup fact. It is required for
    a successful formal Android scan, but it is never promoted to an RF verdict.
    Legacy failed attempts may omit this receipt because they predate the scope.
    """
    document = docs.get("platform/permission-scope.json")
    if result["platform"] != "android":
        _require(
            document is None,
            "iOS evidence cannot carry Android Bluetooth permission scope",
        )
        return
    enabled = (
        request is not None and request.get("authorize_bluetooth_permissions") is True
    )
    if document is None:
        if result["status"] == "passed" and result["platform"] == "android" and enabled:
            raise ScanEvidenceError("successful scan lacks permission scope evidence")
        return
    _require(
        result["platform"] == "android" and enabled,
        "permission scope evidence lacks formal Android authorization",
    )
    _require(
        type(document) is dict
        and document.get("schema_version") == 1
        and document.get("scope") == "android-bluetooth-permissions"
        and document.get("platform") == "android"
        and document.get("device") == result["device"]
        and document.get("run_id") == result["run_id"]
        and document.get("package") == result["app_id"]
        and document.get("user_id") == 0
        and document.get("permission_command_mode") == "root",
        "permission scope identity conflict",
    )
    for field in ("baseline_grants", "prepared_grants", "restored_grants"):
        grants = document.get(field)
        if grants is not None:
            _require(
                type(grants) is dict
                and set(grants) == PERMISSION_GRANTS
                and all(type(value) is bool for value in grants.values()),
                f"invalid permission scope {field}",
            )
    status = document.get("status")
    _require(
        status in {"running", "failed", "passed", "quarantined"},
        "invalid permission scope status",
    )
    if result["status"] == "passed":
        _require(
            status == "passed"
            and document.get("baseline_grants") is not None
            and document.get("prepared_grants")
            == {permission: True for permission in PERMISSION_GRANTS}
            and document.get("restored_grants") == document.get("baseline_grants"),
            "successful scan lacks exact permission restoration",
        )


def _facts(
    attempt: Path,
    result: dict,
    files: list[dict],
    deadline: float,
    scene: SceneEvidence = SCAN_SCENE,
) -> None:
    _result(result, scene=scene)
    docs = _documents(attempt, files, deadline)
    _prepared_facts(docs.get("prepared-result.json"), result, scene)
    request = docs.get("request.json")
    if request is not None:
        _request(request, result, scene)
    elif result["status"] == "passed":
        raise ScanEvidenceError("successful scan lacks stable request")
    _permission_scope_facts(docs, result, request)
    if request is not None or any(name.startswith("fixture/") for name in docs):
        _fixture_facts(attempt, docs, result, request)
    elif any(name.startswith("scan/") for name in docs):
        raise ScanEvidenceError("scan evidence lacks stable fixture lifecycle")
    _phone_facts(attempt, docs, files, result)
    if result["platform"] == "android":
        _candidate_facts(attempt, docs, files, result, deadline)
    else:
        _ios_candidate_facts(attempt, docs, result, deadline)
    _automation_facts(docs, result, docs.get("scan/result.json"))
    _require(
        not any(name.startswith("platform/server-hans-") for name in docs),
        "scene evidence cannot carry UI-automation server receipts",
    )
    _inner_facts(attempt, docs, files, result, request, scene)
    if result["status"] == "passed":
        _require(request is not None, "successful scan lacks request")
        _require(
            result["fixture_status"] == "passed", "successful scan lacks fixture pass"
        )
        required = [
            "scan.run-prepared",
            "scan.result",
            "fixture.close-record",
            "fixture.lease-release",
            "fixture.lease-record",
            "bundle.prepare",
            "cleanup.complete",
        ]
        order = result["cleanup_order"]
        _require(
            all(order.count(label) == 1 for label in required)
            and [order.index(label) for label in required]
            == sorted(order.index(label) for label in required),
            "invalid scan cleanup order",
        )
        inner = docs.get("scan/result.json")
        _require(type(inner) is dict, "successful scan lacks inner result")
        assert isinstance(inner, dict)
        inner_order = inner.get("cleanup_order")
        _require(
            type(inner_order) is list
            and inner_order.count("phone.finish") == 1
            and order.index("fixture.lease-release") > order.index("scan.run-prepared"),
            "fixture lease released before inner phone finalization",
        )
        lease = docs.get("fixture/lease.json")
        _require(lease is not None, "successful scan lacks lease evidence")
        assert lease is not None
        _require(
            lease.get("status") == "released", "successful scan lease was not released"
        )
    else:
        # A failed observation is evidence, not a license to invent absent stage
        # receipts. Validate only documents that actually made it to disk.
        digest = "apk_sha256" if result["platform"] == "android" else "app_tree_sha256"
        for name, doc in docs.items():
            for field, expected in (
                ("run_id", result["run_id"]),
                ("device", result["device"]),
                ("serial", result["device"]),
                ("app_id", result["app_id"]),
                (digest, result[digest]),
            ):
                if field in doc:
                    _require(doc[field] == expected, f"conflicting {field} in {name}")


def publish_scan_bundle(
    attempt: Path,
    result: dict,
    *,
    deadline: float | None = None,
    scene: SceneEvidence = SCAN_SCENE,
) -> Path:
    """Exclusively publish the outer managed result and manifest after fact checks."""
    deadline = _deadline(deadline)
    attempt = _path(attempt)
    raw = json.dumps(result, allow_nan=False).encode()
    _require(len(raw) <= MAX_JSON_BYTES, "result exceeds JSON limit")
    result = evidence_io.strict_json(raw)
    _result(result, scene=scene)
    _require(
        not os.path.lexists(attempt / "result.json")
        and not os.path.lexists(attempt / "manifest.json"),
        "result/manifest already exists",
    )
    before = _snapshot(attempt, deadline)
    _facts(attempt, result, before[0], deadline, scene)
    _require(
        before == _snapshot(attempt, deadline), "evidence drift before publication"
    )
    owned_result: list[tuple[int, int]] = []
    result_digest = publish_new_json(
        attempt / "result.json", result, on_commit=owned_result.append
    )
    sealed = _snapshot(attempt, deadline)
    result_row = next(row for row in sealed[0] if row["path"] == "result.json")
    _require(
        result_row["sha256"] == result_digest
        and sealed[2]["result.json"][:2] == owned_result[0],
        "result publication drift",
    )
    _require(
        [row for row in sealed[0] if row["path"] != "result.json"] == before[0]
        and sealed[1] == before[1]
        and {k: v for k, v in sealed[2].items() if k != "result.json"} == before[2],
        "evidence drift during result publication",
    )
    manifest = {**_summary(result), "files": sealed[0], "directories": sealed[1]}
    _require(
        len(json.dumps(manifest).encode()) <= MAX_JSON_BYTES,
        "manifest exceeds JSON limit",
    )
    _require(
        sealed == _snapshot(attempt, deadline),
        "evidence drift before manifest publication",
    )
    owned_manifest: list[tuple[int, int]] = []
    try:
        manifest_path = attempt / "manifest.json"
        manifest_digest = publish_new_json(
            manifest_path, manifest, on_commit=owned_manifest.append
        )
        manifest_signature = evidence_io.identity(manifest_path.lstat())
        _require(
            manifest_signature[:2] == owned_manifest[0]
            and evidence_io.read_bytes(
                manifest_path, limit=MAX_JSON_BYTES, deadline=deadline
            )[0]
            == manifest_digest,
            "manifest publication drift",
        )
        _require(
            sealed == _snapshot(attempt, deadline),
            "evidence drift after manifest publication",
        )
        _require(
            _verify(attempt, deadline, scene) == result,
            "published scan replay mismatch",
        )
    except BaseException as error:
        try:
            path = attempt / "manifest.json"
            if (
                owned_manifest
                and path.exists()
                and (path.lstat().st_dev, path.lstat().st_ino) == owned_manifest[0]
            ):
                path.unlink()
        except OSError as cleanup:
            error.add_note(f"manifest retraction: {cleanup}")
        raise
    return attempt / "result.json"


def _scene_for(result: dict) -> SceneEvidence:
    """Resolve a sealed bundle's scene from its own stated scope.

    The scope names the producer, so it carries the platform as well as the scene: a bundle
    whose scope names the other platform is rejected instead of being replayed as this one.
    """
    scope = result.get("scope")
    platform = result.get("platform")
    scene = next(
        (
            item
            for item in SCENES.values()
            if platform in MANAGED_PLATFORMS and item.scope(platform) == scope
        ),
        None,
    )
    if scene is None:
        raise ScanEvidenceError(f"unknown managed scene scope: {scope!r}")
    return scene


def _verify(attempt: Path, deadline: float, scene: SceneEvidence | None = None) -> dict:
    manifest_path = attempt / "manifest.json"
    manifest_signature = evidence_io.identity(manifest_path.lstat())
    manifest_digest, _, raw = evidence_io.read_bytes(
        manifest_path, limit=MAX_JSON_BYTES, deadline=deadline
    )
    manifest = evidence_io.strict_json(raw)
    before = _snapshot(attempt, deadline)
    result = evidence_io.read_document(attempt / "result.json", deadline)
    scene = _scene_for(result) if scene is None else scene
    _result(result, scene=scene)
    expected = {**_summary(result), "files": before[0], "directories": before[1]}
    _require(
        json.dumps(manifest, sort_keys=True) == json.dumps(expected, sort_keys=True),
        "manifest inventory/summary mismatch",
    )
    _facts(attempt, result, before[0], deadline, scene)
    _require(
        before == _snapshot(attempt, deadline)
        and evidence_io.identity(manifest_path.lstat()) == manifest_signature
        and evidence_io.read_bytes(
            manifest_path, limit=MAX_JSON_BYTES, deadline=deadline
        )[0]
        == manifest_digest,
        "bundle drift during offline replay",
    )
    return result


def verify_scan_bundle(attempt: Path) -> dict:
    """Verify a movable bundle using local files only; never authorize hardware."""
    return _verify(_path(attempt), _deadline())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify one managed scene bundle offline (Android or iOS)"
    )
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_scan_bundle(args.attempt)
    except Exception as error:  # noqa: BLE001 - CLI reports a bounded verification failure.
        print(f"scan offline integrity verification failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "verification": "offline_integrity",
                "integrity_status": "passed",
                "host_gate_upgraded": False,
                **_summary(result),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
