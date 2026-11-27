"""Synthetic immutable fixture inputs and receipts; no physical attestation."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import cancellation_control as control_api
from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.fault_control import (
    Operation,
    Phase,
    Ticket,
    Winner,
    encode_status,
    owner_nonce,
    ticket_document,
)
from tests.peripherals.nrf52840.support.fault_emulator import read_receipt
from tests.support.rf_lab.markers import SERVICE

ROOT = Path(__file__).resolve().parents[4]


def write_v2_inputs(root: Path, variant: str = "a") -> tuple[Path, Path]:
    """Synthetic test-only digest; never export or authorize a physical image."""
    source = ROOT / "tests/peripherals/nrf52840/firmware"
    contract = json.loads(
        (source / f"images/multifunction-rf-v2-{variant}.json").read_bytes()
    )
    contract.pop("settings")
    profile = json.loads(
        (source / f"profiles/multifunction-rf-v2-{variant}.json").read_bytes()
    )
    contract.update(
        image_sha256="ab" * 32,
        service_uuid=profile["services"][0]["uuid"],
        local_name=profile["advertising"]["local_name"],
    )
    paths = root / "contract.json", root / "profile.json"
    for path, document in zip(paths, (contract, profile), strict=True):
        path.write_text(json.dumps(document), encoding="utf-8")
    return paths


def fake_attempt(root: Path, service: str = SERVICE) -> SimpleNamespace:
    """Create synthetic immutable inputs/receipt, explicitly not physical proof."""
    source = ROOT / "tests/peripherals/nrf52840/firmware"
    contract = json.loads((source / "images/multifunction-rf-v2-a.json").read_bytes())
    contract.pop("settings")
    profile = json.loads((source / "profiles/multifunction-rf-v2-a.json").read_bytes())
    contract["service_uuid"] = service
    contract["local_name"] = profile["advertising"]["local_name"]
    contract["image_sha256"] = "ab" * 32
    profile["advertising"]["service_uuids"] = [service]
    profile["services"][0]["uuid"] = service
    for name, document in (("contract.json", contract), ("profile.json", profile)):
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    coordination = root / "coordination"
    coordination.mkdir()
    with patch.object(
        fixture_session, "identity_for_port", return_value="jlink:123456"
    ):
        plan = fixture_session.prepare_session(
            root / "contract.json",
            root / "profile.json",
            coordination / "fixture-session",
            port="/dev/fake-only",
            service_uuid=service,
        )
    installed = dict(contract)
    installed.pop("fixture_role")
    control_api.write_document(
        plan.receipt_path,
        {
            "schema_version": 1,
            "session_id": plan.session_id,
            "proof_method": fixture_session.PROOF_METHOD,
            "fixture_pid": os.getpid(),
            "fixture_pgid": os.getpgid(0),
            "port": plan.port,
            "resource_identity": plan.resource_identity,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "installed_identity": installed,
        },
    )
    control, _watchdog = control_api.prepare_suite_controls(coordination, plan)
    directory = coordination / control.directory
    directory.mkdir()
    boundaries = []
    ticket = Ticket(
        bytes(range(1, 17)),
        owner_nonce(plan.session_id),
        7,
        1,
        1,
        1,
        Operation.READ,
        51,
    )
    anchor = time.monotonic() - 1
    for index, name in enumerate(
        (
            "armed",
            "held",
            "still-held",
            "resolve-dispatched",
            "read-completed",
            "closed",
        )
    ):
        boundary = {
            "boundary": name,
            "monotonic": anchor + index * 0.1,
            "ticket": ticket_document(ticket),
        }
        if name != "resolve-dispatched":
            phase = (
                Phase.ARMED
                if name == "armed"
                else Phase.RETIRED
                if name in ("read-completed", "closed")
                else Phase.HELD
            )
            boundary["status_wire"] = encode_status(
                read_receipt(
                    ticket,
                    phase,
                    Winner.COMMIT if phase is Phase.RETIRED else Winner.NONE,
                    peer=bytes.fromhex("01665544332211"),
                )
            ).hex()
        if name == "closed":
            boundary.update(hold_inactive=True, peer_count=0)
        elif name != "resolve-dispatched":
            boundary["peer"] = ["112233445566", 1]
        boundaries.append(boundary)
    evidence = {
        "schema_version": 2,
        "control": asdict(control),
        "session_id": plan.session_id,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "resource_identity": plan.resource_identity,
        "fixture_pid": os.getpid(),
        "fixture_pgid": os.getpgid(0),
        "target_uuid": profile["services"][0]["characteristics"][0]["uuid"],
        "target_handle": 51,
        "release_budget_seconds": control_api.RELEASE_BUDGET,
        "status": "passed",
        "boundaries": boundaries,
        "cleanup_errors": [],
    }
    path = directory / "physical-evidence.json"
    control_api.write_document(path, evidence)
    return SimpleNamespace(
        root=coordination,
        plan=plan,
        control=control,
        directory=directory,
        evidence=evidence,
        path=path,
    )
