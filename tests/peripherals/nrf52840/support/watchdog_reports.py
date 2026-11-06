"""Mutually consistent synthetic Peripheral watchdog reports and logs."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from types import SimpleNamespace

from tests.peripherals.nrf52840 import cancellation_control as controls
from tests.peripherals.nrf52840.fault_control import (
    Operation,
    Phase,
    Ticket,
    Winner,
    encode_status,
    owner_nonce,
    ticket_document,
)
from tests.peripherals.nrf52840.fixture_session import load_session_profile
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from tests.peripherals.nrf52840.support.read_fault_emulator import read_receipt
from tests.support.rf_lab.markers import log_parts
from tools.react_native import watchdog_ledger

PEER = bytes.fromhex("01112233445566")


def fake_watchdog(
    attempt: SimpleNamespace, *, anchor: float | None = None
) -> SimpleNamespace:
    """Build one synthetic provider timeline, never native/RF evidence."""
    anchor = time.monotonic() - 13 if anchor is None else anchor
    # Cancellation precedes this connection/ARM in the same provider clock.
    shift = anchor - 1.5 - attempt.evidence["boundaries"][-1]["monotonic"]
    for boundary in attempt.evidence["boundaries"]:
        boundary["monotonic"] += shift
    # A watchdog companion is continuous-mode evidence. Keep standalone
    # fake_attempt reports unchanged, but freeze the preceding preparation here.
    prepared_at = attempt.evidence["boundaries"][0]["monotonic"] - 0.1
    original_peer = attempt.evidence["boundaries"][0]["peer"]
    wire_peer = bytes([original_peer[1]]) + bytes.fromhex(original_peer[0])[::-1]
    attempt.evidence["preparation"] = {
        **{
            key: attempt.evidence[key]
            for key in (
                "control",
                "session_id",
                "contract_sha256",
                "profile_sha256",
                "resource_identity",
                "fixture_pid",
                "fixture_pgid",
                "target_uuid",
                "target_handle",
            )
        },
        "schema_version": 1,
        "status": "prepared",
        "before": "01",
        "expected": "00",
        "readback": "00",
        "boot_nonce": bytes(range(1, 17)).hex(),
        "engine_flags": 0,
        "peer_present": False,
        "peer_count": 0,
        "monotonic": prepared_at,
        "passive_recovery_close": {
            "peer": original_peer,
            "advertising_stopped": True,
            "peer_count": 0,
            "monotonic": prepared_at - 0.1,
            "events": [
                {
                    "opcode": opcode,
                    "index": 0,
                    "payload": (
                        wire_peer
                        + (bytes(6) if opcode == GAP_DEVICE_CONNECTED else b"")
                    ).hex(),
                    "monotonic": prepared_at - 0.5 + index * 0.1,
                }
                for index, opcode in enumerate(
                    (GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED) * 2
                )
            ],
        },
    }
    controls.write_document(attempt.path, attempt.evidence)
    _, control = controls.load_suite_controls(attempt.root)
    directory = attempt.root / control.directory
    directory.mkdir(exist_ok=True)
    profile = load_session_profile(attempt.plan.contract, attempt.plan.profile_path)
    setup, gate, final = (
        part.replace(watchdog_ledger.SERVICE, profile.service_uuid)
        for part in log_parts()
    )
    advertised = f'"advertisedServices": ["{profile.service_uuid}"]'
    setup, gate, final = (
        part.replace(
            advertised, f'"localName": {json.dumps(profile.local_name)}, {advertised}'
        )
        for part in (setup, gate, final)
    )
    marker = lambda kind, phase: (
        "BLEHUB_NITRO_RUNTIME " + control.marker(kind, phase) + "\n"
    )
    dispatch_at = setup.index("BLEHUB_NITRO_RUNTIME rf-watchdog:read-dispatched:")
    prefix = marker("listener", "registered") + setup[:dispatch_at]
    prefix += "BLEHUB_NITRO_RUNTIME rf-watchdog:discovery-ready:connectionId=21007:gattSessionId=21009:characteristicId=30\n"
    prefix += marker("ready", "arm") + marker("observed", "armed")
    prefix += "BLEHUB_NITRO_RUNTIME rf-watchdog:active-dispatched:commandId=21010:connectionId=21007:gattSessionId=21009:characteristicId=30\n"
    prefix += marker("ready", "read-dispatched")
    setup = prefix + marker("observed", "held") + setup[dispatch_at:]
    split = final.index('BLEHUB_NITRO_RUNTIME rf-watchdog:event:{"kind": "scanStarted"')
    after_release = final[:split]
    text = (
        setup
        + gate
        + marker("ready", "release")
        + marker("observed", "released")
        + after_release
        + marker("ready", "recover")
        + marker("observed", "recovery")
        + final[split:]
    )
    boundaries = []

    def record(name: str, instant: float, **fields: object) -> None:
        boundaries.append({"boundary": name, "monotonic": anchor + instant, **fields})

    identity = {"sequence": 8, "handle": attempt.evidence["target_handle"]}
    peer = PEER.hex()
    record("gap", -1, opcode=GAP_DEVICE_CONNECTED, peer=peer)
    ticket = Ticket(
        bytes(range(1, 17)),
        owner_nonce(attempt.plan.session_id),
        8,
        2,
        2,
        1,
        Operation.READ,
        identity["handle"],
    )
    identity = ticket_document(ticket)
    armed = encode_status(read_receipt(ticket, Phase.ARMED, peer=PEER)).hex()
    held = encode_status(read_receipt(ticket, Phase.HELD, peer=PEER)).hex()
    retired = encode_status(
        read_receipt(ticket, Phase.RETIRED, Winner.DISCONNECT, peer=PEER)
    ).hex()
    record("armed", 0, peer=peer, **identity, characteristic="30", status_wire=armed)
    record("held", 0.1, **identity, status_wire=held)
    record("gap", 10.4, opcode=GAP_DEVICE_DISCONNECTED, peer=peer)
    record(
        "retired-after-watchdog",
        10.5,
        peer=peer,
        **identity,
        native_route="30",
        status_wire=retired,
    )
    record(
        "post-retirement-old-route-silence",
        11.6,
        observation_ms=1100,
        commands=["21010", "21011"],
    )
    record("advertising", 11.7)
    record("gap", 11.8, opcode=GAP_DEVICE_CONNECTED, peer=peer)
    record("gap", 11.9, opcode=GAP_DEVICE_DISCONNECTED, peer=peer)
    restored = [
        {
            "role": c.role,
            "handle": identity["handle"] + i,
            "value": c.initial_value.hex(),
            "readback": c.initial_value.hex() if c.permissions & 1 else None,
        }
        for i, c in enumerate(profile.characteristics)
    ]
    evidence = {
        key: attempt.evidence[key]
        for key in (
            "schema_version",
            "session_id",
            "contract_sha256",
            "profile_sha256",
            "resource_identity",
            "fixture_pid",
            "fixture_pgid",
            "target_handle",
            "target_uuid",
        )
    }
    evidence.update(
        control=asdict(control),
        status="passed",
        target_native_permissions=0x43,
        release_budget_seconds=controls.WATCHDOG_RELEASE_BUDGET,
        canonical_values=restored,
        boundaries=boundaries,
        physical_cleanup_errors=[],
        physical_cleanup={
            "hold_inactive": True,
            "advertising_stopped": True,
            "peer_state": "no-known-peers-after-continuous-v2-retirement",
            "status_wire": retired,
            "radio_power": "unchanged",
            "known_peers_remaining": [],
        },
        ledger=watchdog_ledger.verify_ledger(
            text,
            characteristic="30",
            service_uuid=profile.service_uuid,
            integrated=True,
        ),
    )
    path = directory / "physical-evidence.json"
    controls.write_document(path, evidence)
    return SimpleNamespace(
        control=control,
        directory=directory,
        path=path,
        evidence=evidence,
        text=text,
        prefix=prefix,
        setup=setup,
        gate=gate,
        after_release=after_release,
        restored=restored,
        profile=profile,
    )
