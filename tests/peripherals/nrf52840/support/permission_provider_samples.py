"""Pure synthetic permission samples; no OS/device/firmware or RF evidence."""

from __future__ import annotations

import json

from tests.peripherals.nrf52840 import permission_transition as api
from tests.support.rf_lab.markers import IO_MARKERS, SERVICE, ui_database

RUN = "11111111-1111-4111-8111-111111111111"
NAME = "synthetic-permission"
REQUEST_NONCE = "44444444-4444-4444-8444-444444444444"
ROUTE = {"connectionId": "52005", "gattSessionId": "52007", "characteristicId": "30"}


def bootstrap(run: str, phase: str) -> dict:
    index = api.PHASES.index(phase) + 1
    return {
        "run_id": run,
        "phase": phase,
        "pid": 40 + index,
        "uid": 10123,
        "user_id": 0,
        "boot_id": f"22222222-2222-4222-8222-{index:012d}",
        "bundle_id": "com.blehubnitrohost",
        "executable": "file:///synthetic.apk",
        "source": "launch",
        "started_unix_ms": 1700000000000 + index * 1000,
        "started_elapsed_ms": 100000 + index * 1000,
    }


def start(run: str, phase: str) -> list[str]:
    return [
        "native:bootstrap:" + json.dumps(bootstrap(run, phase)),
        f"runtime:runId={run}:phase={phase}:nonce=synthetic-{phase}:state=start",
    ]


def close(run: str, phase: str) -> list[str]:
    return [
        f"{phase}:passed",
        f"runtime:runId={run}:phase={phase}:nonce=synthetic-{phase}:state=close",
        f"passed:phase={phase}:runId={run}",
    ]


def zero(prefix: str) -> str:
    return prefix + json.dumps(dict.fromkeys(api.RESOURCE_FIELDS, 0))


def denied(run: str = RUN) -> list[str]:
    events = [
        {
            "kind": "adapterSnapshotChanged",
            "commandId": "51000",
            "adapterState": "PoweredOn",
            "scanAuthorization": "Denied",
        },
        {"kind": "commandCompleted", "commandId": "51000"},
        {
            "kind": "commandCompleted",
            "commandId": "51002",
            "error": {
                "category": "permission",
                "message": "synthetic permission denial",
                "platformDetails": {
                    "platform": "platform_android",
                    "name": "SecurityException",
                },
            },
        },
    ]
    return [
        *start(run, "denied"),
        *("denied:event:" + json.dumps(e) for e in events),
        zero("denied:resources:"),
        *close(run, "denied"),
    ]


def flow(phase: str, name: str = NAME) -> list[str]:
    label = "baseline" if phase == "active" else "restored"
    markers = [
        f"rf:start:serviceUuid={SERVICE}:expectedLocalName={name}:initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004",
        *IO_MARKERS[:-2],
        "rf:listener:removed",
        "rf:close:resolved",
        *IO_MARKERS[-2:],
    ]
    return [f"{phase}:flow:{label}:" + m for m in markers]


def baseline(run: str = RUN, name: str = NAME) -> list[str]:
    return [
        *start(run, "active"),
        *flow("active", name),
        zero("active:baseline:resources:"),
        "active:baseline-passed",
    ]


def setup(run: str = RUN, name: str = NAME) -> list[str]:
    events = []

    def domain(command: str, kind: str, **fields: object) -> None:
        events.extend(
            [
                dict(kind=kind, commandId=command, **fields),
                {"kind": "commandCompleted", "commandId": command},
            ]
        )

    domain(
        "52000",
        "adapterSnapshotChanged",
        adapterState="PoweredOn",
        scanAuthorization="Authorized",
    )
    domain("52002", "scanStarted", scanSessionId="52001")
    events.append(
        {
            "kind": "advertisementObserved",
            "scanSessionId": "52001",
            "peripheralId": "88",
            "advertisedServices": [SERVICE],
            "localName": name,
            "rssi": -50,
        }
    )
    domain("52003", "scanStopped", scanSessionId="52001")
    events.extend(
        [
            {
                "kind": "connectionStateChanged",
                "commandId": "52004",
                "connectionId": "52005",
                "connectionState": state,
            }
            for state in ("Connecting", "Connected")
        ]
    )
    events.append({"kind": "commandCompleted", "commandId": "52004"})
    database = ui_database("30")
    database[0]["uuid"] = SERVICE
    domain(
        "52006",
        "gattDiscovered",
        connectionId="52005",
        gattSessionId="52007",
        gattServices=database,
        auditSchema="active-gatt-canonical-v1",
        gattSummary={"serviceCount": 1, "characteristicCount": 5, "descriptorCount": 2},
    )
    return [
        *("active:event:" + json.dumps(e, separators=(",", ":")) for e in events),
        f"active:discovery-ready:characteristicId=30:connectionId=52005:gattSessionId=52007:runId={run}",
    ]


def pending(run: str = RUN) -> list[str]:
    snapshot = dict.fromkeys(api.RESOURCE_FIELDS, 0)
    snapshot.update(
        dict.fromkeys(
            (
                "connections",
                "watchdogs",
                "ingressRoots",
                "adapterDelegates",
                "backendLeases",
                "hubRoots",
                "handoffRoots",
                "listeners",
                "lifecycleOwners",
                "peerCacheEntries",
            ),
            1,
        )
    )
    snapshot.update(pendingIo=2, workerThreads=2, gattAttributes=7, callbackRoutes=1)
    return [
        "native:signal:"
        + json.dumps(
            {
                **bootstrap(run, "active"),
                "source": "payload-url",
                "url": api.arm_url(run),
            }
        ),
        f"active:arm-signal:runId={run}:source=linking",
        f"active:reads-accepted:activeCommandId=52008:queuedCommandId=52009:characteristicId=30:connectionId=52005:gattSessionId=52007:runId={run}",
        "active:pending-resources:" + json.dumps(snapshot),
    ]


def restored(run: str = RUN, name: str = NAME) -> list[str]:
    return [
        *start(run, "restored"),
        *flow("restored", name),
        zero("restored:resources:"),
        *close(run, "restored"),
    ]


def replay(groups: list[list[str]] | None = None) -> api.RuntimeFacts:
    facts = api.RuntimeFacts(RUN, SERVICE, NAME)
    (
        denied_markers,
        baseline_markers,
        setup_markers,
        pending_markers,
        restored_markers,
    ) = groups or [denied(), baseline(), setup(), pending(), restored()]
    for group, authorize in (
        (denied_markers, None),
        (baseline_markers, facts.authorize_active),
        (setup_markers, None),
        (pending_markers, facts.authorize_arm),
    ):
        if authorize:
            authorize()
        for marker in group:
            facts.consume(marker)
    facts.mark_waiting()
    facts.confirm_loss()
    facts.authorize_restored()
    for marker in restored_markers:
        facts.consume(marker)
    return facts
