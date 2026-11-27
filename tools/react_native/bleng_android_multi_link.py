"""Managed Android multi-link instrumentation; the host owns every peer.

This lane exists because "two links coexisting, one peer ending, the other still delivering" is
an *engine and backend* property, not a UI one. It therefore drives the formal App's own Nitro
surface from an instrumentation test (the same calls the JS API makes) and owns no Appium, no
screenshot and no page: the observable is delivery and per-link state.

Bounds and ownership that stay with the host:

- every fixture port has its own lease and its own attested session; the run refuses a port whose
  canonical identity differs from the authorisation;
- the fixtures act only on the test process's own stage markers, so a value is applied after the
  App reported that subscription and a peer is ended after the sibling links are established;
- two fixtures are closed independently, each with the controller's own advertising-inactive
  readback, and a single closure failure fails the run;
- one finite budget; no retry, no implicit install of a different APK and no radio, firmware or
  permission mutation.
"""

from __future__ import annotations

import math
import os
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
from tools.android import adb
from tools.android.state_control import AndroidRadioController
from tools.file_digest import sha256_file

from . import android_sdk
from .android_bluetooth_permissions import AndroidBluetoothPermissionScope
from .bleng_android_instrumentation import (
    HOST_MARKER_TAG,
    TEST_COMPONENT,
    MarkerCapture,
    run_instrumentation,
)
from .bleng_multi_link_contract import (
    CLAIM,
    MAXIMUM_PEERS,
    MINIMUM_PEERS,
    READ_WRITE_ROLE,
    UPDATES_ROLE,
    Fault,
    peer_session_id,
    require_rounds,
    write_payload,
)
from .bleng_multi_link_contract import (
    expected_stages as _expected_stages,
)
from .bleng_multi_link_contract import (
    peer_identity as _peer_identity,
)
from .bleng_multi_link_fault import MultiLinkFaultError, WriteRejectFault
from .capture_io import publish_new_json
from .rf_fixture import preflight_fixture
from .scene_targets import read_write_target, subscription_target

SCOPE = "formal-android-native-multi-link"

TEST_CLASS = "com.blehubblenghost.BlengMultiLinkInstrumentedTest"
TEST_PACKAGE = "com.blehubblenghost.test"
APP_PACKAGE = "com.blehubblenghost"
MARKER_TAG = "BLEHUB_RN_MULTI"
METHOD = "twoLinksKeepTheirOwnStateWhenOnePeerEnds"


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _remaining(deadline: float, label: str) -> float:
    budget = deadline - time.monotonic()
    if not math.isfinite(budget) or budget <= 0:
        raise TimeoutError(f"{label} exceeded the run budget")
    return budget


def peer_identity(stage: str) -> str:
    """Re-exported from the shared contract; identity is the whole stage."""
    return _peer_identity(stage)


def _round_log(attempt: Path, round_index: int, rounds: int) -> Path:
    """One instrumentation log per round; a single-round run keeps the documented file name."""
    if rounds == 1:
        return attempt / "instrumentation.log"
    return attempt / f"instrumentation-{round_index}.log"


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
    """One leased, attested fixture plus the facts this run declared for it.

    `started` is set only after this run's own session is live-attested, so cleanup can
    release a peer that failed mid-start without pretending it ever served a link.
    """

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
    started: bool = False


def expected_stages(peer_count: int, *, fault: Fault | None = None) -> tuple[str, ...]:
    """Re-exported from the shared contract; the vocabulary is not platform-specific."""
    return _expected_stages(peer_count, fault=fault)


# Kept so a caller reading only this module still sees which scene it drives.
_write_payload = write_payload


def run_multi_link(
    *,
    device: str,
    app_apk: Path,
    app_apk_sha256: str,
    test_apk: Path,
    test_apk_sha256: str,
    fixtures: Sequence[FixtureSpec],
    output_root: Path,
    timeout: float,
    cleanup_seconds: float,
    authorize_install: bool,
    authorize_fixture_start: bool,
    authorize_bluetooth_permissions: bool,
    use_root: bool,
    cwd: Path,
    run_id: str | None = None,
    rounds: int = 1,
    fault: Fault | None = None,
) -> Path:
    """Run one bounded two-link isolation scene; return the published result path."""
    for name, value, maximum in (
        ("timeout", timeout, 900),
        ("cleanup_seconds", cleanup_seconds, 180),
    ):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(f"{name} must be finite, positive and at most {maximum}")
    if authorize_install is not True or authorize_fixture_start is not True:
        raise ValueError(
            "explicit App/test install and fixture startup authorization required"
        )
    if authorize_bluetooth_permissions is not True:
        raise ValueError(
            "explicit BLUETOOTH_SCAN/CONNECT authorization required: root grant, readback "
            "and exact restore"
        )
    if type(use_root) is not bool:
        raise ValueError("use_root must be an explicit bool")
    if (
        type(device) is not str
        or not device.strip()
        or device.lower().startswith("emulator-")
    ):
        raise ValueError("explicit physical Android serial required")
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
    for label, value in (
        ("app_apk_sha256", app_apk_sha256),
        ("test_apk_sha256", test_apk_sha256),
    ):
        if (
            type(value) is not str
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256")

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
    deadline = time.monotonic() + timeout
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)

    for label, path, expected in (
        ("app", app_apk, app_apk_sha256),
        ("test", test_apk, test_apk_sha256),
    ):
        if not path.is_file():
            raise ValueError(f"missing {label} APK: {path}")
        frozen = attempt / f"{label}.apk"
        with frozen.open("xb") as stream:
            stream.write(path.read_bytes())
        if sha256_file(frozen) != expected:
            raise ValueError(f"{label} APK does not match the authorized SHA-256")

    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "android",
        "device": device,
        "app_id": APP_PACKAGE,
        "test_component": TEST_COMPONENT,
        "test_class": TEST_CLASS,
        "test_method": METHOD,
        "app_apk_sha256": app_apk_sha256,
        "test_apk_sha256": test_apk_sha256,
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
        "instrumentation_output": None,
        "fixture_close": [],
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
        "app_id": APP_PACKAGE,
        "app_apk_sha256": app_apk_sha256,
        "test_apk_sha256": test_apk_sha256,
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
        "authorize_bluetooth_permissions": True,
        "use_root": use_root,
        "timeout": timeout,
        "cleanup_seconds": cleanup_seconds,
        "host_pid": os.getpid(),
        "host_pgid": os.getpgid(0),
    }
    publish_new_json(attempt / "request.json", request)

    phone: AndroidRadioController | None = None
    peers: list[OwnedPeer] = []
    permission_scope: AndroidBluetoothPermissionScope | None = None
    capture: MarkerCapture | None = None
    primary: BaseException | None = None
    try:
        phone = AndroidRadioController(device, run_id, attempt / "phone.json")
        phone.acquire()
        result["phone_status"] = "running"
        _remaining(deadline, "phone lease")

        for index, (spec, service_uuid) in enumerate(
            zip(fixtures, services, strict=True)
        ):
            lease_context = port_lease(
                spec.port, purpose=f"BleHub multi-link peer {index}"
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
                    write_hex=_write_payload(index, provisioned).hex(),
                )
                # Owned before `start`: a peer that fails to come up must release its own
                # lease and serial resources here, and never another peer's.
                peers.append(peer)
                owned = True
                fixture.start(deadline=deadline)
                fixture.require_active_session(plan)
                peer.started = True
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

        def install(label: str, path: Path, *, test_only: bool) -> bool:
            with (attempt / f"install-{label}.log").open(
                "x", encoding="utf-8"
            ) as stream:
                return android_sdk.install_apk(
                    device,
                    path,
                    log=stream,
                    test_only=test_only,
                    use_root=use_root,
                    deadline=deadline,
                    require_owner=phone.require_owner,
                    cwd=cwd,
                )

        installed_with_root = install("app", attempt / "app.apk", test_only=False)
        install("test", attempt / "test.apk", test_only=True)
        result["install_mode"] = {"installedWithRoot": installed_with_root}
        _remaining(deadline, "install")

        (attempt / "platform").mkdir(mode=0o700, exist_ok=True)
        permission_scope = AndroidBluetoothPermissionScope(
            device=device,
            run_id=run_id,
            evidence_path=attempt / "platform/permission-scope.json",
            package=APP_PACKAGE,
            phone_guard=phone.require_owner,
            use_root=use_root,
        )
        permission_scope.prep()
        result["permission_scope"] = "prepared"
        _remaining(deadline, "permissions")

        arguments: dict[str, str] = {
            "blehubMultiLink": "multi-link",
            "blehubPeerCount": str(len(peers)),
        }
        if fault is not None:
            arguments["blehubFault"] = fault.declaration()
        for peer in peers:
            arguments.update(
                {
                    f"peer{peer.index}ServiceUuid": peer.service_uuid,
                    f"peer{peer.index}LocalName": peer.local_name,
                    f"peer{peer.index}ReadWriteUuid": peer.read_write_uuid,
                    f"peer{peer.index}UpdatesUuid": peer.updates_uuid,
                    f"peer{peer.index}ValueHex": peer.value_hex,
                    f"peer{peer.index}WriteHex": peer.write_hex,
                    f"peer{peer.index}Mode": peer.mode,
                }
            )

        def drain_all() -> None:
            for peer in peers:
                peer.fixture.drain_events()

        def index_of(stage: str) -> str:
            return stage.partition(":")[2]

        def apply_value(stage: str, round_index: int) -> None:
            # Applied *after* the App reports the subscription (and, for a survivor, after the
            # sibling link ended): silence here is the defect under test.
            applied = peers[int(index_of(stage))].fixture.publish_notification_sequence(
                UPDATES_ROLE, 1, gap_seconds=0.04
            )
            result["published"].append(
                {
                    "round": round_index,
                    "stage": stage,
                    "count": applied,
                    "value_hex": peers[int(index_of(stage))].value_hex,
                }
            )

        def stage_action(stage: str, round_index: int) -> None:
            name = stage.partition(":")[0]
            if name in ("subscribed", "survivor-requested"):
                apply_value(stage, round_index)
            elif name == "drop-requested":
                peers[int(index_of(stage))].fixture.drop_peer(deadline=deadline)

        result["scenario_status"] = "running"
        expected = expected_stages(len(peers), fault=fault)
        for round_index in range(rounds):
            if round_index:
                # Each round is a complete scene, so every peer has to offer itself again and the
                # previous round's link has to be proven gone first.
                for peer in peers:
                    peer.fixture.prepare_next_cycle(deadline=deadline)
                drain_all()
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
                drain_all()
                if round_fault is not None:
                    round_fault.service()

            # `adb logcat` replays the existing buffer when it attaches, and the previous round's
            # markers would otherwise drive this round's fixtures against links that do not exist
            # yet. A fresh capture writes its own sentinel, so this round begins at that line;
            # nothing is cleared from a buffer this lane does not own.
            capture = MarkerCapture(
                device, deadline, tag=MARKER_TAG, host_tag=HOST_MARKER_TAG
            )
            round_result: dict[str, Any] = {
                "index": round_index,
                "stages": [],
                "status": "failed",
            }
            result["rounds"].append(round_result)
            try:
                output = run_instrumentation(
                    device,
                    arguments,
                    test_class=TEST_CLASS,
                    method=METHOD,
                    expected_stages=expected,
                    capture=capture,
                    on_stage=on_stage,
                    advance=advance,
                    deadline=deadline,
                    require_owner=phone.require_owner,
                    log=_round_log(attempt, round_index, rounds),
                    stage_key=peer_identity,
                )
            except BaseException:
                round_result["stages"] = capture.stages()
                if round_fault is not None:
                    round_result["fault"] = round_fault.evidence()
                raise
            if round_fault is not None and not round_fault.resolved:
                round_result["stages"] = capture.stages()
                round_result["fault"] = round_fault.evidence()
                raise MultiLinkFaultError(
                    "the declared write refusal was never captured inside the run budget"
                )
            round_result["stages"] = output["stages"]
            round_result["summary"] = output["summary"]
            if round_fault is not None:
                round_result["fault"] = round_fault.evidence()
            round_result["status"] = "passed"
            # Each round owns its own logcat relay; the next round starts a fresh one.
            capture.close()
            capture = None
        result["stages"] = output["stages"]
        result["instrumentation_output"] = output["summary"]
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
        cleanup("markers.close", capture.close)

    if primary is not None:

        def dump_device_log() -> None:
            """Keep the App's own view of a failed run; markers alone cannot say why."""
            command = adb.adb_command(device, "logcat", "-d", "-v", "brief")
            output = adb.run_command(command, timeout=60, check=True).stdout
            (attempt / "device-logcat.txt").write_text(output, encoding="utf-8")

        cleanup("device.log", dump_device_log)

    def stop_owned_app() -> None:
        """Bound a stuck instrumentation run to our own installed candidate."""
        path = adb.run_command(
            adb.adb_command(device, "shell", "pm", "path", APP_PACKAGE),
            timeout=10,
            check=True,
        ).stdout.strip()
        installed = path.removeprefix("package:").strip()
        if not installed:
            return
        digest = adb.run_command(
            adb.adb_command(device, "shell", "sha256sum", installed),
            timeout=15,
            check=True,
        ).stdout.split()[0]
        if digest != app_apk_sha256:
            raise RuntimeError("refusing to stop a foreign App installation")
        adb.run_command(
            adb.adb_command(device, "shell", "am", "force-stop", APP_PACKAGE),
            timeout=15,
            check=True,
        )
        result["owned_app_stopped"] = True

    if primary is not None:
        cleanup("app.stop-after-failure", stop_owned_app)
    if permission_scope is not None:
        scope = permission_scope
        cleanup("permissions.restore", lambda: scope.finish(primary))

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
            result["fixture_close"].append({"index": peer.index, **asdict(receipt)})
        else:
            peer.fixture.close_resources()

    for peer in reversed(peers):
        cleanup(f"fixture.{peer.index}.close", lambda peer=peer: close_peer(peer))
    started = sum(1 for peer in peers if peer.started)
    if len(result["fixture_close"]) != started:
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

            result["phone_cleanup_errors"] = phone.finish_app_observation(
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
