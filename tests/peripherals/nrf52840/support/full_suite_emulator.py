"""Synthetic full-suite fixture coordination over the recovery BTP emulator."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import cancellation_control as controls
from tests.peripherals.nrf52840 import full_suite as api
from tests.peripherals.nrf52840 import recovery_ten as recovery
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.fault_control import (
    Opcode,
    decode_status,
    encode_status,
)
from tests.peripherals.nrf52840.gatt_commands import GATT_SET_VALUE
from tests.peripherals.nrf52840.support.recovery_emulator import HANDLES, Harness
from tests.peripherals.nrf52840.support.watchdog_reports import fake_watchdog
from tests.support.rf_lab.markers import CANCELLATION_MARKERS, controlled_markers


class FullHarness:
    """Reuse the strict recovery simulated Central and real BTP dispatcher."""

    def __init__(self, directory: Path, platform: str = "ios") -> None:
        self.h = Harness(directory, platform)
        self.root = self.h.plan.root.parent
        self.h.root = self.root / "recovery"
        api.prepare_control(self.root, self.h.plan)
        cancellation, _ = controls.load_suite_controls(self.root)
        path = self.root / cancellation.directory / "physical-evidence.json"
        self.attempt = SimpleNamespace(
            root=self.root,
            plan=self.h.plan,
            control=cancellation,
            evidence=controls.read_document(path),
            path=path,
        )
        self.watchdog = fake_watchdog(self.attempt, anchor=75.0)
        for item in self.watchdog.evidence["canonical_values"]:
            item["handle"] = HANDLES[item["role"]]
        controls.write_document(self.watchdog.path, self.watchdog.evidence)
        self.fixture = self.h.fixture
        self.base_coordinator = Mock()
        self.values: dict[int, bytes] = {}
        self.fixture._gatt.get_attribute_value = Mock(
            side_effect=lambda handle: SimpleNamespace(
                att_response=0,
                value=self.values[handle],
            )
        )
        with patch.object(api.time, "monotonic", self.h.clock.monotonic):
            self.full = api.FullSuite(self.fixture, self.root, HANDLES)
        self.identity = self.h.identity()
        receipt = next(
            b
            for b in self.watchdog.evidence["boundaries"]
            if b["boundary"] == "retired-after-watchdog"
        )
        self.status = decode_status(bytes.fromhex(receipt["status_wire"]))
        self.ack_fault = ""
        self.ready_bytes: bytes | None = None
        self.send = self.h.wire.send
        self.h.wire.send = self.wire_send
        self.publish = api._publish_receipt
        self.base_lines: list[str] = []
        run = self.h.plan.session_id
        markers = [
            f"rf-lab-full:runtime:session={run}:nonce=synthetic-one-runtime:phase=start",
            f"rf-lab-suite:runtime:session={run}:nonce=synthetic-one-runtime:phase=start",
        ]
        for index, mode in enumerate(api.MODES):
            markers.append(f"rf-lab-suite:flow-started:index={index}:mode={mode}")
            if index == 8:
                markers.extend(controlled_markers(CANCELLATION_MARKERS, cancellation))
            elif index == 9:
                markers.extend(
                    line.removeprefix(api.PREFIX)
                    for line in self.watchdog.text.splitlines()
                )
            markers.append(f"rf-lab-suite:flow-passed:index={index}:mode={mode}")
        markers.extend(
            (
                f"rf-lab-suite:runtime:session={run}:nonce=synthetic-one-runtime:phase=close",
                api.BASE_PASSED,
                f"rf-lab-full:recovery-awaiting:runId={run}",
            )
        )
        self.base_lines = [api.PREFIX + m for m in markers]
        self.write_root()
        append = self.h.append

        def append_and_project(*markers: str) -> None:
            append(*markers)
            self.write_root()

        self.h.append = append_and_project

    def write_root(self) -> None:
        temporary = self.root / "runtime.tmp"
        temporary.write_text(
            "\n".join(self.base_lines + self.h.lines) + "\n", encoding="utf-8"
        )
        temporary.replace(self.root / "runtime.log")

    def wire_send(self, frame: BtpFrame) -> None:
        if frame.service == 255 and frame.opcode == Opcode.STATUS:
            self.h.wire.commands.append(frame)
            self.h.wire.frames.append(
                BtpFrame(255, Opcode.STATUS, 255, encode_status(self.status))
            )
        elif frame.service == 2 and frame.opcode == GATT_SET_VALUE and not self.h.phase:
            # GATT SET_VALUE before recovery is canonical restoration on original handles.
            self.h.wire.commands.append(frame)
            handle, length = struct.unpack("<HH", frame.payload[:4])
            value = frame.payload[4:]
            assert length == len(value)
            self.values[handle] = value
            self.h.wire.frames.append(BtpFrame(2, frame.opcode, 0, b""))
        else:
            self.send(frame)

    def publish_handoff(self, path: Path, document: dict[str, object]) -> None:
        self.publish(path, document)
        if path.name != api.HANDOFF_NAME:
            return
        self.ready_bytes = path.read_bytes()
        value = {**self.identity, "url": self.full.url}
        if self.ack_fault == "wrong-url":
            value["url"] += "?foreign=1"
        if self.ack_fault == "foreign-run":
            value["run_id"] = "11111111-1111-4111-8111-111111111111"
        if self.ack_fault == "bad-pid":
            value["pid"] = True
        signal = api.PREFIX + "rf-lab-full:native:signal:" + json.dumps(value)
        ready = (
            api.PREFIX
            + f"rf-lab-full:recovery-ready:runId={self.h.plan.session_id}:source=linking"
        )
        if self.ack_fault == "missing-native":
            self.base_lines.append(ready)
        elif self.ack_fault != "missing":
            self.base_lines.extend(
                (signal, ready) if self.ack_fault != "reversed" else (ready, signal)
            )
            if self.ack_fault == "duplicate":
                self.base_lines.append(signal)
        self.write_root()

    def run(self) -> None:
        with (
            patch.object(api, "run_continuous", self.base_coordinator),
            patch.object(api.time, "monotonic", self.h.clock.monotonic),
            patch.object(api.time, "sleep", self.h.clock.sleep),
            patch.object(api, "_publish_receipt", side_effect=self.publish_handoff),
            patch.object(recovery, "write_document", side_effect=self.h.progress),
        ):
            self.full.run()
