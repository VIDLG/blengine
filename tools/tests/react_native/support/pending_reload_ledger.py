"""Synthetic pending-reload routes, runtime and physical ledger documents."""

from __future__ import annotations

import copy

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840 import fixture_session, pending_reload

PROBE = "12345678-1234-4234-8234-123456789abc"

FOREIGN = "87654321-4321-4321-8321-cba987654321"

SERVICE = "fd000000-0000-4000-8000-000000000000"

PEER = ["112233445566", 1]

ACTIONS = ("arm-1", "reload", "arm-2")


def route(generation: int) -> str:
    base = 12000 if generation == 1 else 13000
    characteristic = 98765 if generation == 1 else 87654
    return f"commandId={base + 8}:connectionId={base + 4}:gattSessionId={base + 6}:characteristicId={characteristic}"


def native(event: str, run: str) -> str:
    return (
        f"native:{event}:runId={run}:probeId={PROBE}:"
        + route(1)
        + (":source=synthetic:result=closed" if event == "late-submit-rejected" else "")
    )


def synthetic_runtime(run: str) -> list[str]:
    """Exact current App/native vocabulary, including permitted engine overlap."""
    result = []
    for generation in (1, 2):
        base = 12000 if generation == 1 else 13000
        prefix = f"generation={generation}:"
        result.extend(
            prefix + marker
            for marker in (
                f"runtime-start:runId={run}:nonce=synthetic-engine-{generation}",
                "arm-linking-listener:registered-before-initialize",
                "listener:registered-before-initialize",
            )
        )
        if generation == 2:
            result.extend(
                native(event, run)
                for event in (
                    "late-submit-rejected",
                    "engine-close-complete",
                )
            )
        result.extend(
            prefix + marker
            for marker in (
                "native-ready:passed",
                f"initialize:passed:commandId={base}",
                f"scan:passed:serviceUuid={SERVICE}:peripheralId=88:scanSessionId={base + 1}",
                f"connected:passed:commandId={base + 5}:connectionId={base + 4}:peripheralId=88",
                f"gatt:passed:commandId={base + 7}:connectionId={base + 4}:gattSessionId={base + 6}",
                "awaiting-arm:" + route(generation),
                "arm-js-observed:source=linking:url=matched",
            )
        )
        if generation == 1:
            result.append(native("read-accepted", run))
        result.append(
            prefix
            + "read-dispatched:"
            + route(generation)
            + (":peripheralId=88" if generation == 1 else "")
        )
        if generation == 1:
            result.extend(
                native(event, run)
                for event in (
                    "engine-invalidation-close-started",
                    "dispose-returned",
                )
            )
            result.append(prefix + "cleanup-started")
        else:
            result.extend(
                (
                    prefix + "read:passed:" + route(2) + ":value=00",
                    prefix + "close:passed",
                    f"passed:runId={run}",
                )
            )
    return [pending_reload.PREFIX + marker for marker in result]


def synthetic_physical(plan, lines: list[str], identity: tuple[int, int]) -> dict:
    """Build provider-shaped records; these are explicitly not physical evidence."""
    profile = fixture_session.load_session_profile(plan.contract, plan.profile_path)
    rw = next(c for c in profile.characteristics if c.role == "read-write")
    records = []
    tickets = {
        sequence: fault.Ticket(
            bytes(range(1, 17)),
            fault.owner_nonce(plan.session_id),
            sequence,
            sequence - 6,
            sequence - 6,
            1,
            fault.Operation.READ,
            51,
        )
        for sequence in (7, 8)
    }

    def status(sequence, phase, *, disconnected=False):
        retired = phase is fault.Phase.RETIRED
        captured = phase is not fault.Phase.ARMED
        flags = fault.StatusFlags.REQUEST_CAPTURED if captured else fault.StatusFlags(0)
        if retired:
            flags |= fault.StatusFlags.REARMABLE
        if retired and not disconnected:
            flags |= (
                fault.StatusFlags.NORMAL_PATH_EVALUATED
                | fault.StatusFlags.CALLBACK_INVOKED
                | fault.StatusFlags.RESPONSE_QUEUED
                | fault.StatusFlags.ANSWER_ORDERED
                | fault.StatusFlags.TX_COMPLETED
            )
        value = fault.Status(
            tickets[sequence],
            phase,
            fault.Winner.DISCONNECT
            if disconnected
            else fault.Winner.COMMIT
            if retired
            else fault.Winner.NONE,
            flags,
            0x0A if captured else 0,
            0,
            0,
            bytes([PEER[1]]) + bytes.fromhex(PEER[0])[::-1],
            0,
            0,
            b"",
            b"\x00",
            b"\x00",
            fault.RefCounters(0, 0, 0, 0, 0, 0)
            if retired
            else fault.RefCounters(1, 1, 1, 1, 0, 0),
            fault.Remaining(
                10000 if not captured else 0,
                20000 if phase is fault.Phase.HELD else 0,
                0,
                0,
            ),
            fault.Outcome.DISCONNECTED_UNCOMMITTED
            if disconnected
            else fault.Outcome.NORMAL_NO_COMMIT
            if retired
            else fault.Outcome.PENDING,
            fault.InfrastructureError.NONE,
        )
        return fault.encode_status(value).hex()

    def record(
        name: str, generation: int, at: float, *, peer=PEER, sequence=None, **fields
    ) -> None:
        value = {
            "boundary": name,
            "generation": generation,
            "monotonic": at,
            "peer": copy.deepcopy(peer),
            **fields,
        }
        if sequence is not None:
            value.update(fault.ticket_document(tickets[sequence]))
        records.append(value)

    for generation in (1, 2):
        offset = 100.0 + (generation - 1) * 10
        sequence = 6 + generation
        record(
            "gap-connected",
            generation,
            offset,
            peer=None,
            sequence=7 if generation == 2 else None,
            observed_peer=copy.deepcopy(PEER),
        )
        record(
            "armed",
            generation,
            offset + 1,
            sequence=sequence,
            armed_at=offset + 0.9,
            status_wire=status(sequence, fault.Phase.ARMED),
            route=route(generation),
        )
        dispatch = (
            pending_reload.PREFIX
            + f"generation={generation}:read-dispatched:"
            + route(generation)
            + (":peripheralId=88" if generation == 1 else "")
        )
        record(
            "waiting",
            generation,
            offset + 2,
            sequence=sequence,
            status_wire=status(sequence, fault.Phase.HELD),
            first_waiting_at=offset + 1.9,
            read_dispatched=dispatch,
        )
        if generation == 1:
            record(
                "native-dispose-observed",
                1,
                offset + 3,
                sequence=sequence,
                probe_id=PROBE,
            )
        if generation == 1:
            record(
                "gap-disconnected",
                1,
                offset + 3.5,
                sequence=sequence,
                observed_peer=copy.deepcopy(PEER),
            )
            record(
                "retired",
                1,
                offset + 4,
                sequence=sequence,
                status_wire=status(sequence, fault.Phase.RETIRED, disconnected=True),
            )
            record("advertising-recovered", 1, offset + 8, peer=None, sequence=sequence)
        else:
            record(
                "before-resolve",
                2,
                offset + 4,
                sequence=sequence,
                status_wire=status(sequence, fault.Phase.HELD),
            )
            record(
                "resolve-requested",
                2,
                offset + 5,
                sequence=sequence,
                resolve_at=offset + 4.9,
            )
            record(
                "retired",
                2,
                offset + 6,
                sequence=sequence,
                status_wire=status(sequence, fault.Phase.RETIRED),
            )
            record(
                "gap-disconnected",
                2,
                offset + 7,
                sequence=sequence,
                observed_peer=copy.deepcopy(PEER),
            )
    cleanup = {
        "hold_inactive": True,
        "hold_state": "RETIRED",
        "status_wire": status(8, fault.Phase.RETIRED),
        "no_known_peers": True,
        "advertising_stopped": True,
    }
    record("physical-cleanup", 2, 118.0, sequence=8, **cleanup)
    record("closed", 2, 119.0, sequence=8, hold_inactive=True, peer_count=0)
    return {
        "schema_version": 2,
        "status": "passed",
        "run_id": plan.session_id,
        "session_id": plan.session_id,
        "fixture_pid": identity[0],
        "fixture_pgid": identity[1],
        "profile_id": profile.profile_id,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "resource_identity": plan.resource_identity,
        "live_receipt_path": str(plan.receipt_path),
        "port": plan.port,
        "service_uuid": SERVICE,
        "target_uuid": rw.uuid,
        "target_handle": 51,
        "target_permissions": {"profile": 3, "expected_native": 0x43, "native": 0x43},
        "budgets_seconds": {
            "whole": 180.0,
            "phase": 30.0,
            "arm_to_held": 8.0,
            "resolve_to_retired": 3.0,
            "native_close": 9.0,
        },
        "boundaries": records,
        "cleanup_errors": [],
        "physical_cleanup": cleanup,
        "native_close_facts": {
            "native:" + event: pending_reload.PREFIX + native(event, plan.session_id)
            for event in pending_reload.NATIVE_EVENTS
        },
        "runtime_nonces": {"1": "synthetic-engine-1", "2": "synthetic-engine-2"},
    }
