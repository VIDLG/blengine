"""Synthetic adapter-transition ledger, control and physical-sidecar samples."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from tests.support.rf_lab.markers import IO_MARKERS, SERVICE
from tools.react_native import adapter_transition_ledger as ledger
from tools.react_native.host_contract import ANDROID_PACKAGE
from tools.react_native.rf_evidence_bundle import RECOVERY_RESOURCE_FIELDS

RUN = "12345678-1234-4123-8123-123456789abc"

BOOT = "12345678-1234-4123-8123-123456789abd"

NONCE = "m1234-abcd-efgh"

LOG = Path("adapter-transition-runtime.log")

DEVICE = "synthetic-device-not-RF"

BOOTSTRAP = {
    "run_id": RUN,
    "pid": 1234,
    "boot_id": BOOT,
    "bundle_id": ANDROID_PACKAGE,
    "executable": "file:/data/app/synthetic/base.apk",
    "source": "launch",
}

OFF_EVENTS = [
    {
        "kind": "adapterSnapshotChanged",
        "commandId": "31000",
        "adapterState": "PoweredOff",
        "scanAuthorization": "Authorized",
    },
    {"kind": "commandCompleted", "commandId": "31000"},
    {
        "kind": "commandCompleted",
        "commandId": "31002",
        "error": {"category": "adapter_unavailable", "message": "adapter off"},
    },
]


def bodies() -> list[str]:
    """The actual outer protocol and all reports of a successful rf-io flow."""
    zero = json.dumps(dict.fromkeys(RECOVERY_RESOURCE_FIELDS, 0))
    flow = [
        f"rf:start:serviceUuid={SERVICE}:expectedLocalName=BleHubNrf52840:initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004",
        *IO_MARKERS[:-2],
        "rf:listener:removed",
        "rf:close:resolved",
        *IO_MARKERS[-2:],
    ]
    return [
        "native:bootstrap:" + json.dumps(BOOTSTRAP),
        f"runtime:runId={RUN}:nonce={NONCE}:phase=start",
        *("off:event:" + json.dumps(event) for event in OFF_EVENTS),
        "off:resources:" + zero,
        "off:passed",
        f"awaiting-on:runId={RUN}",
        "native:signal:"
        + json.dumps(
            {
                **BOOTSTRAP,
                "source": "payload-url",
                "url": f"blehub-rn-spike://adapter/{RUN}/on",
            }
        ),
        f"on-signal:runId={RUN}:source=linking",
        "on:started",
        *("on:flow:" + line for line in flow),
        "on:resources:" + zero,
        "on:passed",
        f"runtime:runId={RUN}:nonce={NONCE}:phase=close",
        f"passed:runId={RUN}",
    ]


def transcript(markers: list[str] | None = None, *, newline: str = "\n") -> str:
    """Threadtime-like prefixes are opaque; only native payloads supply IDs."""
    return (
        "host diagnostic"
        + newline
        + "".join(
            "10-15 12:00:00.000 1234 5678 I Test: " + ledger.PREFIX + body + newline
            for body in (bodies() if markers is None else markers)
        )
        + "unrelated trailing diagnostic"
        + newline
    )


def state_control() -> dict:
    """Synthetic host receipts, not real subprocess/adapter evidence."""
    actions = []
    for index, (label, state) in enumerate(
        (("initial", "ON"), ("off", "OFF"), ("on", "ON"), ("final", "ON"))
    ):
        argv = ["adb", "-s", DEVICE, "shell"]
        commands = []
        if label in ("off", "on"):
            commands.extend(
                [
                    (
                        argv
                        + [
                            "svc",
                            "bluetooth",
                            "disable" if label == "off" else "enable",
                        ],
                        "",
                    ),
                    (
                        argv + ["dumpsys", "bluetooth_manager"],
                        "  state: TURNING_OFF\n"
                        if label == "off"
                        else "  state: TURNING_ON\n",
                    ),
                ]
            )
        commands.append(
            (
                argv + ["dumpsys", "bluetooth_manager"],
                f"Bluetooth Status\r\n    state: {state}\r\n",
            )
        )
        base = index * 10
        actions.append(
            {
                "label": label,
                "requested_state": state,
                "observed_state": state,
                "started_monotonic": base,
                "confirmed_monotonic": base + len(commands) + 0.5,
                "receipts": [
                    {
                        "command": cmd,
                        "returncode": 0,
                        "stdout": stdout,
                        "started_monotonic": base + i,
                        "finished_monotonic": base + i + 0.25,
                    }
                    for i, (cmd, stdout) in enumerate(commands)
                ],
            }
        )
    return {
        "schema_version": 1,
        "platform": "android",
        "device": DEVICE,
        "run_id": RUN,
        "status": "passed",
        "initial_state": "ON",
        "final_state": "ON",
        "actions": actions,
        "primary_error": None,
        "cleanup_errors": [],
    }


def readiness_sidecar(runtime: dict, receipt_sha256: str) -> dict:
    """Synthetic provider ready declaration with no live no-peer claim."""
    return {
        "schema_version": 1,
        "run_id": runtime["run_id"],
        "status": "ready",
        "receipt_sha256": receipt_sha256,
        "native_bootstrap": copy.deepcopy(runtime["native_bootstrap"]),
        "monotonic": 99.0,
    }


def physical_sidecars(runtime: dict) -> tuple[dict, dict, dict]:
    """Provider's real JSON shape populated solely with synthetic facts."""
    request = {
        "schema_version": 1,
        "session_id": RUN,
        "contract_sha256": "a" * 64,
        "profile_sha256": "b" * 64,
        "resource_identity": "serial:synthetic-fixture",
        "service_uuid": SERVICE,
        "port": "/dev/synthetic-not-a-device",
    }
    receipt = {key: value for key, value in request.items() if key != "service_uuid"}
    receipt.update(
        proof_method="btp-vendor-live-under-device-lease",
        fixture_pid=12345,
        fixture_pgid=12345,
        captured_at_utc="2026-10-15T00:00:00+00:00",
        installed_identity={"synthetic": "not live attestation"},
    )
    events = [
        {
            "kind": "connected",
            "peer": ["123456789abc", 1],
            "payload_hex": "01bc9a78563412" + "00" * 6,
            "monotonic": 101.0,
        },
        {
            "kind": "disconnected",
            "peer": ["123456789abc", 1],
            "payload_hex": "01bc9a78563412",
            "monotonic": 102.0,
        },
    ]
    physical = {
        key: value
        for key, value in request.items()
        if key not in ("service_uuid", "port")
    }
    physical.update(
        mode="adapter-transition-attested",
        status="passed",
        run_id=RUN,
        fixture_pid=12345,
        fixture_pgid=12345,
        receipt_sha256=hashlib.sha256(json.dumps(receipt).encode()).hexdigest(),
        connections=1,
        disconnections=1,
        events=events,
        boundaries=[
            {"boundary": "gap-" + event["kind"], "cycle": -1, **event}
            for event in events
        ],
        hold_status={"protocol_version": 2, "engine_flags": 0},
        cleanup={"advertising_stopped": True, "hold_inactive": True, "peer_count": 0},
        cleanup_errors=[],
        primary_error=None,
        rejected_events=[],
        resources=copy.deepcopy(runtime["closed_resources"]),
        native_bootstrap=copy.deepcopy(runtime["native_bootstrap"]),
        native_signal=copy.deepcopy(runtime["native_signal"]),
        handles={"synthetic": 1},
    )
    physical["readiness"] = readiness_sidecar(runtime, physical["receipt_sha256"])
    physical["boundaries"].insert(
        0,
        {
            "boundary": "on-window-ready",
            "cycle": -1,
            "monotonic": physical["readiness"]["monotonic"],
            "hold_inactive": True,
            "peer_count": 0,
        },
    )
    physical["go_sha256"] = hashlib.sha256(
        json.dumps({"schema_version": 1, "run_id": RUN}).encode()
    ).hexdigest()
    physical["on-started"] = 100.0
    return request, receipt, physical


def active_physical_sidecars(runtime: dict) -> tuple[dict, dict, dict]:
    """Complete synthetic WAITING/request/authorize/confirm/release physical record."""
    from tests.peripherals.nrf52840.adapter_transition import (
        ARM_NAME,
        LOSS_NAME,
        OFF_CONFIRMED,
        OFF_READY,
        OFF_REQUEST,
    )

    request, receipt, physical = physical_sidecars(runtime)
    from tests.peripherals.nrf52840.fault_control import (
        Operation,
        Phase,
        Ticket,
        Winner,
        owner_nonce,
        status_document,
        ticket_document,
    )
    from tests.peripherals.nrf52840.support.read_fault_emulator import read_receipt

    ticket = Ticket(
        bytes(range(1, 17)),
        owner_nonce(runtime["run_id"]),
        7,
        1,
        1,
        1,
        Operation.READ,
        51,
    )
    identity = ticket_document(ticket)
    peer = physical["events"][0]["peer"]
    base = {
        "schema_version": 1,
        "adapter_mode": "active-loss",
        "run_id": runtime["run_id"],
        "receipt_sha256": physical["receipt_sha256"],
        "native_bootstrap": runtime["native_bootstrap"],
        "ticket": identity,
        "peer": peer,
    }
    armed_status = status_document(
        read_receipt(
            ticket, Phase.ARMED, peer=bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1]
        )
    )
    held_status = status_document(
        read_receipt(
            ticket, Phase.HELD, peer=bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1]
        )
    )
    retired_status = status_document(
        read_receipt(
            ticket,
            Phase.RETIRED,
            Winner.DISCONNECT,
            peer=bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1],
        )
    )
    armed = {
        **base,
        "status": armed_status,
        "phase": "armed",
        "route": runtime["active_loss"]["route"],
    }
    waiting = {
        **base,
        "status": held_status,
        "phase": "waiting",
        "route": runtime["active_loss"]["route"],
    }
    host = {
        "schema_version": 1,
        "adapter_mode": "active-loss",
        "run_id": runtime["run_id"],
    }

    def digest(value: dict) -> str:
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    off_request = {**host, "loss_ready_sha256": digest(waiting)}
    authorized = {
        **base,
        "phase": "off-authorized",
        "status": held_status,
        "request_sha256": digest(off_request),
    }
    confirmed = {**host, "off_ready_sha256": digest(authorized)}
    physical.update(
        adapter_mode="active-loss",
        connections=2,
        disconnections=2,
        active_protocol={
            ARM_NAME: armed,
            LOSS_NAME: waiting,
            OFF_REQUEST: off_request,
            OFF_READY: authorized,
            OFF_CONFIRMED: confirmed,
        },
        hold_status=retired_status,
        handles={"read-write": 51},
    )
    physical["readiness"]["adapter_mode"] = "active-loss"
    links = [
        {**e, "monotonic": t}
        for e, t in zip(physical["events"], (90.1, 90.65), strict=True)
    ] + physical["events"]
    physical["events"] = links

    def boundary(name: str, moment: float, **fields: object) -> dict:
        return {"boundary": name, "cycle": -1, "monotonic": moment, **fields}

    def gap(event: dict) -> dict:
        return {"boundary": "gap-" + event["kind"], "cycle": -1, **event}

    physical["boundaries"] = [
        boundary("active-window-ready", 90.0),
        gap(links[0]),
        boundary("active-armed", 90.2, ticket=identity, status=armed_status),
        boundary(
            "active-waiting", 90.3, ticket=identity, status=held_status, peer=peer
        ),
        boundary(
            "active-off-authorized",
            90.4,
            ticket=identity,
            status=held_status,
            peer=peer,
        ),
        boundary("active-off-confirmed", 90.5, ticket=identity),
        boundary("active-settled", 90.6, policy=runtime["active_loss"]["policy"]),
        gap(links[1]),
        boundary("active-retired", 90.7, ticket=identity, status=retired_status),
        boundary("on-window-ready", 99.0, hold_inactive=True, peer_count=0),
        gap(links[2]),
        gap(links[3]),
    ]
    return request, receipt, physical


def active_gatt_event(*, compact: bool = True) -> dict:
    """Production-shaped Android canonical + GAP/GATT/DIS topology (4/21/3)."""
    characteristics = [
        {
            "id": str(20 + i),
            "uuid": prefix + "0000-0000-4000-8000-000000000000",
            "readable": i != 4,
            "writableWithResponse": i in (0, 4),
            "writableWithoutResponse": i in (0, 4),
            "notifiable": i == 1,
            "indicatable": i in (1, 2),
            "descriptors": [
                {"id": str(30 + i), "uuid": "00002902-0000-1000-8000-00805f9b34fb"}
            ]
            if i in (1, 2)
            else [],
        }
        for i, prefix in enumerate(("fff1", "fff2", "fff5", "fff3", "fff4"))
    ]
    canonical = {
        "id": "19",
        "uuid": SERVICE,
        "primary": True,
        "characteristics": characteristics,
    }

    def readable(identifier: int, uuid: str) -> dict:
        return {
            "id": str(identifier),
            "uuid": f"0000{uuid}-0000-1000-8000-00805f9b34fb",
            "readable": True,
            "writableWithResponse": False,
            "writableWithoutResponse": False,
            "notifiable": False,
            "indicatable": False,
            "descriptors": [],
        }

    services = [
        canonical,
        {
            "id": "60",
            "uuid": "00001800-0000-1000-8000-00805f9b34fb",
            "primary": True,
            "characteristics": [
                readable(100, "2a00"),
                readable(101, "2a01"),
                readable(106, "2a04"),
            ],
        },
        {
            "id": "61",
            "uuid": "00001801-0000-1000-8000-00805f9b34fb",
            "primary": True,
            "characteristics": [
                {
                    **readable(102, "2a05"),
                    "readable": False,
                    "indicatable": True,
                    "descriptors": [
                        {"id": "200", "uuid": "00002902-0000-1000-8000-00805f9b34fb"}
                    ],
                },
                *[
                    readable(i, u)
                    for i, u in ((103, "2b29"), (104, "2b2a"), (105, "2b3a"))
                ],
            ],
        },
        {
            "id": "62",
            "uuid": "0000180a-0000-1000-8000-00805f9b34fb",
            "primary": True,
            "characteristics": [
                readable(110 + i, u)
                for i, u in enumerate(
                    (
                        "2a29",
                        "2a24",
                        "2a25",
                        "2a27",
                        "2a26",
                        "2a28",
                        "2a23",
                        "2a50",
                        "2a2a",
                    )
                )
            ],
        },
    ]
    return {
        "kind": "gattDiscovered",
        "commandId": "32006",
        "connectionId": "32005",
        "gattSessionId": "32007",
        "gattServices": [canonical] if compact else services,
        **(
            {
                "auditSchema": "active-gatt-canonical-v1",
                "gattSummary": {
                    "serviceCount": len(services),
                    "characteristicCount": sum(
                        len(s["characteristics"]) for s in services
                    ),
                    "descriptorCount": sum(
                        len(c["descriptors"])
                        for s in services
                        for c in s["characteristics"]
                    ),
                },
            }
            if compact
            else {}
        ),
    }


def active_bodies(
    policy: str = "disconnect-first", *, run: str = RUN, name: str = "BleHubNrf52840"
) -> list[str]:
    """Synthetic accepted reads and real-shaped failure events; not native/RF execution."""
    boot = {**BOOTSTRAP, "run_id": run, "adapter_mode": "active-loss"}
    setup = [
        {
            "kind": "adapterSnapshotChanged",
            "commandId": "32000",
            "adapterState": "PoweredOn",
            "scanAuthorization": "Authorized",
        },
        {"kind": "commandCompleted", "commandId": "32000"},
        {"kind": "scanStarted", "commandId": "32002", "scanSessionId": "32001"},
        {"kind": "commandCompleted", "commandId": "32002"},
        {
            "kind": "advertisementObserved",
            "scanSessionId": "32001",
            "peripheralId": "42",
            "localName": name,
            "advertisedServices": [SERVICE],
        },
        {"kind": "scanStopped", "commandId": "32003", "scanSessionId": "32001"},
        {"kind": "commandCompleted", "commandId": "32003"},
        *(
            {
                "kind": "connectionStateChanged",
                "commandId": "32004",
                "connectionId": "32005",
                "connectionState": state,
            }
            for state in ("Connecting", "Connected")
        ),
        {"kind": "commandCompleted", "commandId": "32004"},
        active_gatt_event(),
        {"kind": "commandCompleted", "commandId": "32006"},
    ]

    def error(category: str) -> dict:
        return {
            "category": category,
            "message": "synthetic native error",
            "platformDetails": {
                "platform": "platform_android",
                "name": "BluetoothGatt",
                "code": "133",
            },
        }

    failures = [
        {
            "kind": "commandCompleted",
            "commandId": "32008",
            "error": error("connection" if policy == "disconnect-first" else "gatt"),
        },
        {
            "kind": "commandCompleted",
            "commandId": "32009",
            "error": error("connection"),
        },
        {
            "kind": "connectionStateChanged",
            "connectionId": "32005",
            "error": error("connection"),
        },
    ]
    retired = dict.fromkeys(RECOVERY_RESOURCE_FIELDS, 0)
    retired.update(
        dict.fromkeys(
            (
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
    retired["workerThreads"] = 2
    original = bodies()
    tail = original[5:]
    tail = [
        m.replace(RUN, run).replace(
            "expectedLocalName=BleHubNrf52840", f"expectedLocalName={name}"
        )
        for m in tail
    ]
    tail[3] = "native:signal:" + json.dumps(
        {
            **boot,
            "source": "payload-url",
            "url": f"blehub-rn-spike://adapter/{run}/active-loss/on",
        }
    )
    return [
        "native:bootstrap:" + json.dumps(boot),
        f"runtime:runId={run}:nonce={NONCE}:phase=start",
        *("active:event:" + json.dumps(e) for e in setup),
        "active:discovery-ready:"
        + json.dumps(
            {
                "connectionId": "32005",
                "gattSessionId": "32007",
                "characteristicId": "20",
            }
        ),
        "native:arm:"
        + json.dumps(
            {
                **boot,
                "source": "payload-url",
                "url": f"blehub-rn-spike://adapter/{run}/active-loss/arm",
            }
        ),
        f"active:arm-signal:runId={run}:source=linking",
        "active:accepted:32008",
        "active:accepted:32009",
        *("active:event:" + json.dumps(e) for e in failures),
        f"active:settled:{policy}",
        "active:retired:" + json.dumps(retired),
        "active:silence:passed",
        *tail,
    ]
