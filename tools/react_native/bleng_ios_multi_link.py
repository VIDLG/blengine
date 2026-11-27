"""Managed iOS multi-link instrumentation; the host owns every peer.

The same scene as the Android lane (`bleng_multi_link_contract`), driven from inside the formal
App process by a hosted XCTest bundle instead of `am instrument`. Nothing about the claim is
iOS-specific; what is iOS-specific is only how the host talks to the test:

- the peer topology is written into the staged `.xctestrun` as test-process environment
  variables, so the signed test bundle needs no rebuild per topology;
- the test's stage markers are read from the device syslog (`idevicesyslog -u`), the analogue of
  `adb logcat`; markers are diagnostics and never a result;
- every fixture action is a BLE fact (a notification value or a peer-side link end), so there is
  no host→test command channel and nothing for the test to poll.

Bounds and ownership that stay with the host: one phone lease, one lease and one attested session
per declared peer, one finite budget, an explicit signed App plus test bundle, and one close
receipt per fixture.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.direct_fixture import (
    AdvertisingCloseReceipt,
    DirectFixture,
)
from tests.peripherals.nrf52840.resource_lease import canonical_identity, port_lease
from tools import host_command

from .bleng_ios_native_common import (
    APPLE_NOTIFICATION_DIAGNOSTICS_VARIABLE,
    SyslogMarkerCapture,
    app_bundle_id,
    require_executable,
    resolve_xctestrun,
    stage_environment,
    staged_xctestrun,
    tree_sha256,
)
from .bleng_multi_link_contract import (
    CLAIM,
    MARKER_TAG,
    MAXIMUM_PEERS,
    MINIMUM_PEERS,
    PROBE_TAG,
    READ_WRITE_ROLE,
    UPDATES_ROLE,
    Fault,
    expected_stages,
    peer_identity,
    peer_session_id,
    require_rounds,
    write_payload,
)
from .bleng_multi_link_fault import MultiLinkFaultError, WriteRejectFault
from .capture_io import publish_new_json
from .ios_radio_lease import NATIVE_MULTI_LINK_SCOPE, IosRadioLease
from .ios_sdk import xcodebuild_test_without_building
from .rf_fixture import preflight_fixture
from .scene_targets import read_write_target, subscription_target

SCOPE = "formal-ios-native-multi-link"
# The hosted device test bundle this lane selects. A manifest states both spellings: the bundle
# (`Something.xctest`) and the target (`-only-testing` takes the target name). The target name is
# read back from the manifest rather than repeated here, so the two cannot drift apart.
#
# This is the device-only target on purpose: the app-hosted `BleHubBlengHostNativeTests` target
# links its own copy of the Nitro pod, and a second registration of the same HybridObject aborts
# the process.
TEST_BUNDLE = "BleHubBlengHostDeviceTests.xctest"
TEST_CLASS = "BlengMultiLinkDeviceTests"
METHOD = "testTwoLinksKeepTheirOwnStateWhenOnePeerEnds"
SYSTEM_TIMEOUT_MAX = 1800
CLEANUP_TIMEOUT_MAX = 240
# A value is applied over a bounded window: enough for the App's lane to bind and for the radio
# to carry a frame, small enough that the scene still fails fast when delivery is broken.
VALUE_APPLICATIONS = 5
VALUE_GAP_SECONDS = 0.2


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _remaining(deadline: float, label: str) -> float:
    budget = deadline - time.monotonic()
    if not math.isfinite(budget) or budget <= 0:
        raise TimeoutError(f"{label} exceeded the run budget")
    return budget


@dataclass(frozen=True)
class FixtureSpec:
    """One declared peer the host owns, in the order the test addresses it."""

    port: str
    contract: Path
    profile: Path
    resource_identity: str

    def __post_init__(self) -> None:
        if not self.port.strip():
            raise ValueError("fixture port must not be empty")
        kind, _, serial = self.resource_identity.partition(":")
        if not serial or canonical_identity(kind, serial) != self.resource_identity:
            raise ValueError("canonical stable fixture identity required")
        for label, path in (("contract", self.contract), ("profile", self.profile)):
            if not path.is_file():
                raise ValueError(f"missing fixture {label}: {path}")


@dataclass
class OwnedPeer:
    """One leased, attested fixture plus the facts this run declared for it."""

    index: int
    spec: FixtureSpec
    lease_context: Any
    plan: fixture_session.FixtureSession
    fixture: DirectFixture
    service_uuid: str
    local_name: str
    read_write_uuid: str
    updates_uuid: str
    mode: str
    value_hex: str
    write_hex: str
    address: str = ""
    started: bool = False


def declared_peers(peers: Sequence[OwnedPeer]) -> str:
    """The test-process declaration: the topology, and nothing the test could infer."""
    return json.dumps(
        [
            {
                "serviceUuid": peer.service_uuid,
                "localName": peer.local_name,
                "readWriteUuid": peer.read_write_uuid,
                "updatesUuid": peer.updates_uuid,
                "valueHex": peer.value_hex,
                "writeHex": peer.write_hex,
                "mode": peer.mode,
            }
            for peer in peers
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class PreparedApp:
    """The facts about the App and its manifest a run depends on, all read before it leases."""

    bundle_id: str
    tree_sha256: str
    test_target: str


def _preflight(*, app: Path, app_tree_sha256: str, xctestrun: Path) -> PreparedApp:
    """Every fact this run depends on, checked before it takes a lease.

    A wrong test host, a stale manifest, an App that never embedded the test bundle or a missing
    host tool all surface today only after a phone lease, an App install and a fixture startup.
    None of these checks touches the phone, so checking them first makes a wrong input cost
    nothing to discover.
    """
    if not app.is_dir() or app.is_symlink():
        raise ValueError(f"missing signed App directory: {app}")
    if not (app / "embedded.mobileprovision").is_file():
        raise ValueError("App is not signed")
    if not xctestrun.is_file() or xctestrun.is_symlink():
        raise ValueError(f"missing xctestrun: {xctestrun}")
    for name in ("xcrun", "idevicesyslog"):
        require_executable(name)
    bundle_id = app_bundle_id(app)
    measured = tree_sha256(app)
    if app_tree_sha256 and measured != app_tree_sha256:
        raise ValueError("signed App tree does not match the authorized SHA-256")
    target = resolve_xctestrun(
        xctestrun,
        test_bundle=TEST_BUNDLE,
        expected_app=app,
        expected_host_bundle_id=bundle_id,
    )
    if not target.bundle.is_dir() or target.bundle.is_symlink():
        raise ValueError(
            f"the App does not embed the test bundle this lane selects: {target.bundle}"
        )
    return PreparedApp(
        bundle_id=bundle_id,
        tree_sha256=measured,
        test_target=target.name,
    )


def run_multi_link(
    *,
    device: str,
    app: Path,
    app_tree_sha256: str,
    xctestrun: Path,
    fixtures: Sequence[FixtureSpec],
    output_root: Path,
    timeout: float,
    cleanup_seconds: float,
    authorize_install: bool,
    authorize_fixture_start: bool,
    cwd: Path,
    run_id: str | None = None,
    diagnose_notifications: bool = False,
    rounds: int = 1,
    fault: Fault | None = None,
) -> Path:
    """Run one bounded multi-link isolation scene on a physical iPhone.

    Everything this run depends on — a signed App of the authorized tree, a manifest that hosts
    and embeds this lane's test bundle, and the host tools that drive it — is checked before the
    phone lease, so a wrong input is refused without costing an attempt.
    """
    for name, value, maximum in (
        ("timeout", timeout, SYSTEM_TIMEOUT_MAX),
        ("cleanup_seconds", cleanup_seconds, CLEANUP_TIMEOUT_MAX),
    ):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(f"{name} must be finite, positive and at most {maximum}")
    if authorize_install is not True or authorize_fixture_start is not True:
        raise ValueError(
            "explicit signed-App install and fixture startup authorization required"
        )
    if type(device) is not str or not device.strip():
        raise ValueError("explicit physical iPhone UDID required")
    if (
        not isinstance(fixtures, Sequence)
        or not MINIMUM_PEERS <= len(fixtures) <= MAXIMUM_PEERS
    ):
        raise ValueError(
            f"fixtures must be {MINIMUM_PEERS}..{MAXIMUM_PEERS} declared peers"
        )
    ports = [spec.port for spec in fixtures]
    identities = [spec.resource_identity for spec in fixtures]
    if len(set(ports)) != len(ports) or len(set(identities)) != len(identities):
        raise ValueError("each declared peer needs its own port and identity")
    if app_tree_sha256 and re.fullmatch(r"[0-9a-f]{64}", app_tree_sha256) is None:
        raise ValueError("app_tree_sha256 must be a lowercase SHA-256")
    services = [
        preflight_fixture(fixture_contract=spec.contract, fixture_profile=spec.profile)
        for spec in fixtures
    ]
    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    rounds = require_rounds(rounds)
    if fault is not None and fault.peer >= len(fixtures):
        raise ValueError(
            f"declared fault peer {fault.peer} is not one of the {len(fixtures)} declared peers"
        )
    prepared = _preflight(app=app, app_tree_sha256=app_tree_sha256, xctestrun=xctestrun)
    deadline = time.monotonic() + timeout
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)

    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "ios",
        "device": device,
        "app_id": prepared.bundle_id,
        "app_tree_sha256": prepared.tree_sha256,
        "test_bundle": TEST_BUNDLE,
        "test_target": prepared.test_target,
        "test_class": TEST_CLASS,
        "test_method": METHOD,
        "status": "failed",
        "rf": CLAIM,
        "host_gate_status": "failed",
        "scenario_status": "not_run",
        "phone_status": "not_acquired",
        "fixture_status": "not_acquired",
        "rounds": [],
        "peers": [],
        "stages": [],
        "published": [],
        "fault": fault.declaration() if fault is not None else None,
        "primary_error": None,
        "primary_traceback": None,
        "cleanup_errors": [],
        "phone_cleanup_errors": [],
        "cleanup_order": [],
    }
    request = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "device": device,
        "app_tree_sha256": prepared.tree_sha256,
        "fixtures": [
            {
                "index": index,
                "port": spec.port,
                "contract": spec.contract.name,
                "profile": spec.profile.name,
                "resource_identity": spec.resource_identity,
            }
            for index, spec in enumerate(fixtures)
        ],
        "authorize_install": True,
        "authorize_fixture_start": True,
        "timeout": timeout,
        "cleanup_seconds": cleanup_seconds,
        "host_pid": os.getpid(),
        "host_pgid": os.getpgid(0),
    }
    publish_new_json(attempt / "request.json", request)

    phone: IosRadioLease | None = None
    peers: list[OwnedPeer] = []
    capture: SyslogMarkerCapture | None = None
    primary: BaseException | None = None
    try:
        phone = IosRadioLease(
            device,
            run_id,
            attempt / "phone.json",
            scope=NATIVE_MULTI_LINK_SCOPE,
        )
        phone.acquire()
        result["phone_status"] = "running"
        _remaining(deadline, "phone lease")

        for index, (spec, service_uuid) in enumerate(
            zip(fixtures, services, strict=True)
        ):
            lease_context = port_lease(
                spec.port, purpose=f"BleHub iOS multi-link peer {index}"
            )
            owned = False
            try:
                lease = lease_context.__enter__()
                kind, _, serial = spec.resource_identity.partition(":")
                lease.require(kind, serial)
                if lease.identity != spec.resource_identity:
                    raise RuntimeError(
                        f"peer {index} port differs from the authorized identity"
                    )
                plan = fixture_session.prepare_session(
                    spec.contract,
                    spec.profile,
                    attempt / f"fixture-{index}",
                    port=spec.port,
                    service_uuid=service_uuid,
                    session_id=peer_session_id(run_id, index),
                )
                read_write_uuid, provisioned = read_write_target(plan, READ_WRITE_ROLE)
                updates_uuid, mode, value_hex = subscription_target(plan, UPDATES_ROLE)
                fixture = DirectFixture(
                    plan.port, plan.profile_path, lease=lease, fixture_session=plan
                )
                peer = OwnedPeer(
                    index=index,
                    spec=spec,
                    lease_context=lease_context,
                    plan=plan,
                    fixture=fixture,
                    service_uuid=service_uuid,
                    local_name=plan.contract.local_name,
                    read_write_uuid=read_write_uuid,
                    updates_uuid=updates_uuid,
                    mode=mode,
                    value_hex=value_hex,
                    write_hex=write_payload(index, provisioned).hex(),
                )
                peers.append(peer)
                owned = True
                fixture.start(deadline=deadline)
                fixture.require_active_session(plan)
                peer.started = True
                # The peer's own address is evidence a passive sniffer can follow into the
                # connection; it is read back from the controller, not assumed.
                peer.address = fixture.local_address()
                _remaining(deadline, f"peer {index} start")
            except BaseException:
                if not owned:
                    lease_context.__exit__(None, None, None)
                raise
        result["fixture_status"] = "passed"
        result["peers"] = [
            {
                "index": peer.index,
                "port": peer.spec.port,
                "resource_identity": peer.spec.resource_identity,
                "address": peer.address,
                "session_id": peer.plan.session_id,
                "contract": peer.plan.contract.fixture_role,
                "service_uuid": peer.service_uuid,
                "local_name": peer.local_name,
                "read_write_uuid": peer.read_write_uuid,
                "updates_uuid": peer.updates_uuid,
                "mode": peer.mode,
                "value_hex": peer.value_hex,
                "write_hex": peer.write_hex,
            }
            for peer in peers
        ]

        staged = staged_xctestrun(
            xctestrun,
            attempt / "multi-link.xctestrun",
            test_bundle=TEST_BUNDLE,
            environment={
                **stage_environment(
                    declaration=declared_peers(peers),
                    fault=fault.declaration() if fault is not None else None,
                ),
                # An opt-in capture of the Apple backend's own notification decisions; it is
                # read from the run's xcodebuild log, never used to decide the scene.
                **(
                    {APPLE_NOTIFICATION_DIAGNOSTICS_VARIABLE: "1"}
                    if diagnose_notifications
                    else {}
                ),
            },
            expected_app=app,
            expected_host_bundle_id=prepared.bundle_id,
        )
        result["notification_diagnostics"] = diagnose_notifications
        _remaining(deadline, "xctestrun staging")

        def drain() -> None:
            for peer in peers:
                peer.fixture.drain_events()

        def apply_value(stage: str, round_index: int) -> None:
            """Apply the host's value for one stage, and record what was asked for.

            The host cannot know when the App's lane is bound, and it cannot see the value cross
            the air. So it applies the value over a bounded window instead of once: the claim is
            "the App receives a value the host applied", not "the first frame survived a race with
            the App's own enable completion".
            """
            applied = 0
            for _ in range(VALUE_APPLICATIONS):
                applied += peers[
                    int(index_of(stage))
                ].fixture.publish_notification_sequence(
                    UPDATES_ROLE, 1, gap_seconds=VALUE_GAP_SECONDS
                )
            result["published"].append(
                {
                    "round": round_index,
                    "stage": stage,
                    "count": applied,
                    "value_hex": peers[int(index_of(stage))].value_hex,
                }
            )

        def index_of(stage: str) -> str:
            return stage.partition(":")[2]

        def stage_action(stage: str, round_index: int) -> None:
            name = stage.partition(":")[0]
            if name in ("subscribed", "survivor-requested"):
                apply_value(stage, round_index)
            elif name == "drop-requested":
                peers[int(index_of(stage))].fixture.drop_peer(deadline=deadline)

        capture = SyslogMarkerCapture(device, deadline, tag=MARKER_TAG)
        # Attach before launching, and let the relay leave its startup window: a marker emitted
        # into a relay that is not live yet is lost, and a missing prefix then looks like "the
        # scene never started".
        capture.wait_live(deadline)
        result["marker_relay"] = {
            "attach_seconds": capture.attach_seconds(),
            "warmup_seconds": capture.warmup_seconds,
        }
        result["scenario_status"] = "running"
        expected = expected_stages(len(peers), fault=fault)
        for round_index in range(rounds):
            if round_index:
                # Each round is a complete scene, so every peer has to offer itself again and the
                # previous round's link has to be proven gone first.
                for peer in peers:
                    peer.fixture.prepare_next_cycle(deadline=deadline)
                drain()
                _remaining(deadline, f"round {round_index} rearm")

            # A fault is armed as soon as its link is up: before discovery, hence before the
            # App's first write, so the hold exists when that write arrives.
            round_fault = (
                WriteRejectFault(
                    peers[fault.peer].fixture,
                    payload=bytes.fromhex(peers[fault.peer].write_hex),
                )
                if fault is not None
                else None
            )

            def on_stage(
                stage: str,
                round_index: int = round_index,
                round_fault: WriteRejectFault | None = round_fault,
            ) -> None:
                if (
                    round_fault is not None
                    and fault is not None
                    and stage == f"connected:{fault.peer}"
                ):
                    round_fault.arm()
                stage_action(stage, round_index)

            def advance(round_fault: WriteRejectFault | None = round_fault) -> None:
                drain()
                if round_fault is not None:
                    round_fault.service()

            offset = len(capture.stages())
            round_result: dict[str, Any] = {
                "index": round_index,
                "stages": [],
                "status": "failed",
            }
            result["rounds"].append(round_result)
            command = xcodebuild_test_without_building(
                xctestrun=staged,
                device=device,
                result=_result_bundle(attempt, round_index, rounds),
                only_testing=f"{prepared.test_target}/{TEST_CLASS}/{METHOD}",
                # A diagnostic run keeps the whole test log: the Apple backend's records live
                # there.
                quiet=not diagnose_notifications,
            )
            try:
                _drive_test(
                    command,
                    capture=capture,
                    expected=expected,
                    on_stage=on_stage,
                    advance=advance,
                    deadline=deadline,
                    log=_round_log(attempt, round_index, rounds),
                    cwd=cwd,
                    stage_offset=offset,
                )
            except BaseException:
                round_result["stages"] = capture.stages()[offset:]
                if round_fault is not None:
                    round_result["fault"] = round_fault.evidence()
                raise
            if round_fault is not None and not round_fault.resolved:
                round_result["stages"] = capture.stages()[offset:]
                round_result["fault"] = round_fault.evidence()
                raise MultiLinkFaultError(
                    "the declared write refusal was never captured inside the run budget"
                )
            round_result["stages"] = capture.stages()[offset:]
            if round_fault is not None:
                round_result["fault"] = round_fault.evidence()
            round_result["status"] = "passed"
        result["scenario_status"] = "passed"
        result["status"] = "passed"
        result["host_gate_status"] = "pending"
    except BaseException as error:  # noqa: BLE001 -- finish owned cleanup before propagating
        primary = error
        result["primary_error"] = _error(error)
        # A host-side failure the operator can read: which call raised, not just its message.
        result["primary_traceback"] = traceback.format_exception(error)
        if result["scenario_status"] == "running":
            result["scenario_status"] = "failed"

    cleanup_deadline = time.monotonic() + cleanup_seconds

    def cleanup(label: str, action: Callable[[], None]) -> bool:
        result["cleanup_order"].append(label)
        try:
            action()
            return True
        except BaseException as error:  # noqa: BLE001 -- continue independent cleanup
            result["cleanup_errors"].append(f"{label}: {_error(error)}")
            return False

    if capture is not None:

        def snapshot_markers() -> None:
            """Keep the relay's own view: a missing prefix is only visible in the raw lines."""
            result["stages"] = capture.stages()
            # The test's own probe burst: how much of the opening window the relay carried.
            result.setdefault("marker_relay", {})["probe_lines"] = (
                capture.lines_containing(PROBE_TAG)
            )
            (attempt / "markers.txt").write_text(capture.log(), encoding="utf-8")

        cleanup("markers.snapshot", snapshot_markers)
        cleanup("markers.close", capture.close)
    if primary is not None:

        def dump_device_log() -> None:
            """Keep the device's own view of a failed run; markers alone cannot say why.

            `idevicesyslog` relays until it is stopped, so this reads for a bounded window and
            then terminates it. A blocking wait here would turn every failure into a cleanup
            error, and a cleanup error is exactly what forbids releasing the phone lease.
            """
            process = subprocess.Popen(
                ["idevicesyslog", "-u", device],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                output, _ = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=5)
            (attempt / "device-syslog.txt").write_text(output or "", encoding="utf-8")

        cleanup("device.log", dump_device_log)

    def close_peer(peer: OwnedPeer) -> None:
        # Each peer is closed on its own: one link's residue must not hide another's receipt.
        if peer.started:
            peer.fixture.settle_no_peer(deadline=cleanup_deadline)
            receipt = peer.fixture.stop_advertising_and_close(deadline=cleanup_deadline)
            if (
                type(receipt) is not AdvertisingCloseReceipt
                or receipt.session_id != peer.plan.session_id
                or receipt.resource_identity != peer.spec.resource_identity
            ):
                raise RuntimeError(
                    f"peer {peer.index} passive close receipt is foreign"
                )
            result.setdefault("fixture_close", []).append(
                {"index": peer.index, **asdict(receipt)}
            )
        else:
            peer.fixture.close_resources()

    for peer in reversed(peers):
        cleanup(f"fixture.{peer.index}.close", lambda peer=peer: close_peer(peer))
    started = sum(1 for peer in peers if peer.started)
    if len(result.get("fixture_close", [])) != started:
        result["fixture_status"] = "failed"

    for peer in reversed(peers):

        def release(peer: OwnedPeer = peer) -> None:
            peer.lease_context.__exit__(None, None, None)

        cleanup(f"fixture.{peer.index}.lease-release", release)

    if phone is not None:
        result["phone_cleanup_errors"] = []

        def finish_phone() -> None:
            assert phone is not None

            def authorize_release() -> None:
                if result["cleanup_errors"]:
                    raise RuntimeError("; ".join(result["cleanup_errors"]))

            result["phone_cleanup_errors"] = phone.finish_observation(
                primary, finalize_owned=authorize_release
            )

        cleanup("phone.finish", finish_phone)
        result["phone_status"] = phone.document["status"]

    if result["cleanup_errors"] or result["phone_cleanup_errors"]:
        result["status"] = "failed"
        result["host_gate_status"] = "failed"
    path = attempt / "result.json"
    publish_new_json(path, result)
    if primary is not None:
        raise primary
    return path


def _result_bundle(attempt: Path, round_index: int, rounds: int) -> Path:
    """One result bundle per round; a single-round run keeps the documented file name."""
    if rounds == 1:
        return attempt / "multi-link.xcresult"
    return attempt / f"multi-link-{round_index}.xcresult"


def _round_log(attempt: Path, round_index: int, rounds: int) -> Path:
    """One xcodebuild log per round; the single-round run keeps `xcodebuild.log`."""
    if rounds == 1:
        return attempt / "xcodebuild.log"
    return attempt / f"xcodebuild-{round_index}.log"


def _drive_test(
    command: list[str],
    *,
    capture: SyslogMarkerCapture,
    expected: tuple[str, ...],
    on_stage: Callable[[str], None],
    advance: Callable[[], None],
    deadline: float,
    log: Path,
    cwd: Path,
    stage_offset: int = 0,
) -> dict[str, Any]:
    """Run one bounded xcodebuild while advancing fixtures on the test's own markers.

    `stage_offset` is where this round's stages begin in the relay's own buffer: the relay stays
    attached across rounds, so a repeated scene is a slice, not a second capture.
    """
    advanced: set[str] = set()
    started = time.monotonic()
    with log.open("x", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            while process.poll() is None:
                _remaining(deadline, "xcodebuild")
                _advance(
                    capture.stages()[stage_offset:],
                    advanced,
                    stream,
                    on_stage,
                    advance,
                )
                time.sleep(0.1)
        finally:
            _advance(
                capture.stages()[stage_offset:],
                advanced,
                stream,
                on_stage,
                advance,
            )
            host_command.stop(process, grace=10)
            output = (process.stdout.read() if process.stdout else "") or ""
            stream.write(output)
    stages = capture.stages()[stage_offset:]
    if [stage for stage in stages if stage in expected] != list(expected):
        raise RuntimeError(
            "instrumentation stages differ from the scene contract: "
            f"expected {list(expected)!r}; observed {stages!r}"
        )
    # The return code is the verdict. `-quiet` suppresses the textual success banner, and a test
    # run that produced all its stages but failed an assertion still exits non-zero.
    if process.returncode:
        raise RuntimeError(
            f"xcodebuild exited {process.returncode}: {_output_tail(output)}"
        )
    return {
        "stages": stages,
        "summary": {
            "returncode": process.returncode,
            "elapsed_seconds": time.monotonic() - started,
            "stages": stages,
        },
    }


def _output_tail(text: str, *, lines: int = 20) -> str:
    """The last meaningful lines of a test log, so a non-zero exit explains itself."""
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(kept[-lines:]) or "no output"


def _advance(
    stages: list[str],
    advanced: set[str],
    stream: Any,
    on_stage: Callable[[str], None],
    advance: Callable[[], None],
) -> None:
    for stage in stages:
        identity = peer_identity(stage)
        if identity in advanced:
            continue
        advanced.add(identity)
        stream.write(f"[marker] {stage}\n")
        stream.flush()
        advance()
        on_stage(identity)
