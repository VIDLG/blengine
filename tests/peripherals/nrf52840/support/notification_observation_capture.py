"""Explicit synthetic host-integration samples; NOT a C-wire or RF crossing."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840 import notification_observation as wire
from tests.peripherals.nrf52840 import notification_observation_capture as capture
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.peripherals.nrf52840.fixture_session import load_session
from tests.peripherals.nrf52840.gatt_commands import GATT_SET_VALUE
from tests.peripherals.nrf52840.support import (
    notification_observation_samples as samples,
)
from tests.peripherals.nrf52840.support.read_fault_emulator import PEER


@contextmanager
def synthetic_profile_admission():
    """Explicit pending-parent boundary, NOT a production capability admission test.

    fixture_session currently still rejects optional capabilities. The existing
    profile validator is exercised on the unchanged base family; only these
    synthetic observer tests cross that pending admission boundary. Remove this
    adapter when the parent owns the complete profile/session admission update.
    """
    original = fixture_session.load_session_profile

    def profile(contract, path):
        base = replace(
            contract, capabilities=contract.capabilities - {capture.CAPABILITY}
        )
        return original(base, path)

    with patch.object(fixture_session, "load_session_profile", side_effect=profile):
        yield


def opt_in(plan: Any) -> Any:
    """Rewrite synthetic inputs/receipt only; never touch tracked firmware inputs."""
    contract_path = plan.root / "contract.json"
    contract = json.loads(contract_path.read_bytes())
    contract["capabilities"].append(capture.CAPABILITY)
    write_document(contract_path, contract)
    digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    request = json.loads(plan.request_path.read_bytes())
    request["contract_sha256"] = digest
    write_document(plan.request_path, request)
    receipt = json.loads(plan.receipt_path.read_bytes())
    receipt["contract_sha256"] = digest
    receipt["captured_at_utc"] = datetime.now(UTC).isoformat()
    receipt["installed_identity"]["capabilities"] = contract["capabilities"]
    write_document(plan.receipt_path, receipt)
    return load_session(plan.request_path)


def attach(
    harness: Any, *, owner: bytes = samples.OWNER, generation: int = 7
) -> ObserverWire:
    plan = opt_in(harness.plan)
    harness.plan = harness.fixture._fixture_session = plan
    harness.owner = type(harness.owner)(harness.fixture, harness.root, harness.handles)
    capture.prepare_request(harness.root, plan)
    return ObserverWire(harness, owner=owner, generation=generation)


class ObserverWire:
    """Extend an existing synthetic BTP transport without creating another session."""

    def __init__(
        self, harness: Any, *, owner: bytes = samples.OWNER, generation: int = 7
    ) -> None:
        self.harness = harness
        self.target = harness.wire
        self.original_send = self.target.send
        self.owner, self.generation = owner, generation
        self.enabled = owner != wire.ZERO_OWNER
        self.records: list[dict[str, Any]] = []
        self.read_counts: dict[int, int] = {}
        self.commands: list[Any] = []
        self.configure_count = 0
        self.fail_configure = 0
        self.fail_read = False
        self.omit = False
        self.target.send = self.send

    def query(self) -> bytes:
        return samples.query_bytes(
            owner=self.owner,
            owner_generation=self.generation,
            enabled=int(self.enabled),
            last_sequence=len(self.records),
            oldest_sequence=1 if self.records else 0,
            newest_sequence=len(self.records),
            count=len(self.records),
        )

    def send(self, frame: Any) -> None:
        if frame.service == 255 and frame.opcode in (
            wire.QUERY,
            wire.CONFIGURE,
            wire.READ,
        ):
            self.commands.append(frame)
            self.target.commands.append(frame)
            if frame.opcode == wire.CONFIGURE:
                self.configure_count += 1
                if self.configure_count == self.fail_configure:
                    self.target.reply(frame, bytes([1, wire.Result.NOT_READY]))
                    return
                request = frame.payload
                assert request[1:17] == samples.BOOT
                assert request[17:33] == self.owner
                assert int.from_bytes(request[33:37], "little") == self.generation
                new = request[37:53]
                if self.owner != new:
                    assert (
                        self.owner == wire.ZERO_OWNER
                        or not self.enabled
                        and not request[-1]
                    )
                    self.generation += 1
                self.owner, self.enabled = new, bool(request[-1])
            if frame.opcode == wire.READ:
                if self.fail_read:
                    self.target.reply(frame, bytes([1, wire.Result.NOT_FOUND]))
                    return
                sequence = int.from_bytes(frame.payload[33:37], "little")
                record = self.records[sequence - 1]
                assert frame.payload[17:33] == record["owner"]
                self.read_counts[sequence] = self.read_counts.get(sequence, 0) + 1
                # A deferred batch binding has no PDU/TX facts on its first read.
                factory = (
                    samples.record_bytes
                    if self.read_counts[sequence] == 1
                    else samples.completed_bytes
                )
                if record["value"] == b"\xf0":
                    silent = dict(record)
                    silent["selected_peer"] = bytes(7)
                    payload = samples.record_bytes(
                        **silent,
                        ccc_flags=7,
                        cached_ccc=0,
                        aggregate_ccc=0,
                        selected_local_id=0,
                        per_peer_ccc=0,
                        subscriber_count=0,
                        decision=4,
                        flags=1,
                        entry_index=0,
                        times=(10, 11, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                    )
                else:
                    payload = factory(**record)
            else:
                payload = self.query()
            self.target.reply(frame, payload)
            return
        if (
            frame.service == 2
            and frame.opcode == GATT_SET_VALUE
            and self.enabled
            and not self.omit
        ):
            value = frame.payload[4:]
            self.records.append(
                {
                    "owner": self.owner,
                    "owner_generation": self.generation,
                    "sequence": len(self.records) + 1,
                    "connection_epoch": self.target.epoch,
                    "bearer_epoch": self.target.epoch,
                    "database_epoch": self.target.database,
                    "live_peer": PEER,
                    "selected_peer": PEER,
                    "value_handle": int.from_bytes(frame.payload[:2], "little"),
                    "value_length": len(value),
                    "copied_length": len(value),
                    "value": value,
                }
            )
        self.original_send(frame)
