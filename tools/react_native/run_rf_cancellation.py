"""Standalone cancellation diagnostic on the existing App/barrier and leased v2 owner."""

from __future__ import annotations

import re
import time

from tests.peripherals.nrf52840.cancellation_control import RELEASE_BUDGET
from tests.peripherals.nrf52840.fault_control import (
    Decision,
    Phase,
    Status,
    read_completed,
    status_document,
    ticket_document,
)
from tests.peripherals.nrf52840.gap_commands import GAP_DEVICE_CONNECTED
from tools.react_native.check_runtime_log import check_rf_cancellation
from tools.react_native.run_rf_watchdog import Experiment, Runtime, WatchdogError, main
from tools.react_native.watchdog_ledger import verify_control


class CancellationExperiment(Experiment):
    """Keep cancellation semantics separate from the watchdog/disconnect experiment."""

    def read_status(self) -> Status:
        if self.fault is None or self.ticket is None or self.peer is None:
            raise WatchdogError("cancellation lacks its exact read ticket/peer")
        status = self.fault.status(self.ticket)
        if (
            status.peer != self.peer
            or status.before != b"\x00"
            or status.after != b"\x00"
        ):
            raise WatchdogError("cancellation STATUS peer/before/after drift")
        return status

    def verify_runtime(self, runtime: Runtime, text: str) -> None:
        check_rf_cancellation(
            text,
            self.profile.service_uuid,
            runtime.log,
            expected_local_name=self.profile.local_name,
        )
        verify_control(text, runtime.control, complete=True)
        projected = re.findall(
            r"BLEHUB_NITRO_RUNTIME (rf-cancellation:event:[^\r\n]*)", text
        )
        if (
            len(projected) != 1
            or ":commandId=9028|9027|9027|9029|9029:" not in projected[0]
            or not projected[0].endswith(":value=none|00|none|00|none")
        ):
            raise WatchdogError(
                "held cancellation requires exact Cancelled/read/recovery evidence"
            )

    def run(self, runtime: Runtime, target: int) -> None:
        if (
            self.fault is None
            or runtime.control is None
            or runtime.control.mode != "rf-cancellation"
        ):
            raise WatchdogError("cancellation requires its exact v2 owner/control")
        self.strict_peer_events = True
        self.runtime_started = True
        self.advertise()
        runtime.launch()
        runtime.wait_control_request("arm", time.monotonic() + 60)
        drain_deadline = time.monotonic() + 2
        while self.events(poll=True):
            if time.monotonic() >= drain_deadline:
                raise WatchdogError("cancellation pre-ARM GAP drain deadline")
        if len(self.peers) != 1:
            raise WatchdogError("cancellation ARM requires one discovered peer")
        self.peer = next(iter(self.peers))
        before = self.gatt.get_attribute_value(target)
        rw = next(c for c in self.profile.characteristics if c.role == "read-write")
        if before.att_response != 0 or before.value != rw.initial_value:
            raise WatchdogError("cancellation before value drift")
        self.armed_at = time.monotonic()
        deadline = self.armed_at + RELEASE_BUDGET
        self.ticket = self.fault.arm_read(target, before.value, self.peer).ticket
        status = self.read_status()
        self.events()
        if status.phase is not Phase.ARMED or self.peers != {self.peer}:
            raise WatchdogError("cancellation ARM route/phase drift")
        self.record(
            "armed",
            monotonic=self.armed_at,
            ticket=ticket_document(self.ticket),
            status=status_document(status),
        )
        runtime.signal("armed")
        runtime.wait_control_request("read-dispatched", deadline)
        while True:
            self.guard()
            status = self.read_status()
            self.events()
            if time.monotonic() >= deadline or self.peers != {self.peer}:
                raise WatchdogError("cancellation capture deadline/peer drift")
            if status.phase is Phase.HELD:
                break
            if status.phase is not Phase.ARMED:
                raise WatchdogError("cancellation did not reach HELD")
            self.pause()
        self.record(
            "held", ticket=ticket_document(self.ticket), status=status_document(status)
        )
        runtime.signal("held")
        runtime.wait_control_request("release", deadline)
        text = runtime.snapshot()
        marker = "rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled"
        if re.findall(r"BLEHUB_NITRO_RUNTIME ([^\r\n]*)", text).count(marker) != 1:
            raise WatchdogError(
                "cancellation resolution requires exact queued Cancelled terminal"
            )
        status = self.read_status()
        self.events()
        if (
            time.monotonic() >= deadline
            or status.phase is not Phase.HELD
            or self.peers != {self.peer}
        ):
            raise WatchdogError("cancellation RESOLVE lacks fresh HELD/peer/budget")
        self.record(
            "still-held",
            ticket=ticket_document(self.ticket),
            status=status_document(status),
        )
        self.fault.resolve(self.ticket, Decision.COMMIT)
        self.record("resolve-dispatched", ticket=ticket_document(self.ticket))
        resolution_deadline = min(deadline, time.monotonic() + 2)
        while True:
            status = self.read_status()
            self.events()
            if time.monotonic() >= resolution_deadline or self.peers != {self.peer}:
                raise WatchdogError(
                    "cancellation normal read resolution deadline/peer drift"
                )
            if read_completed(status):
                break
            if status.phase is Phase.RETIRED:
                raise WatchdogError("cancellation read is not NORMAL_NO_COMMIT")
            self.pause()
        self.record(
            "read-completed",
            ticket=ticket_document(self.ticket),
            status=status_document(status),
        )
        runtime.signal("released")
        deadline = time.monotonic() + 60
        while True:
            text = runtime.snapshot(allow_exit=True)
            self.events(poll=True)
            if time.monotonic() >= deadline:
                raise WatchdogError("cancellation native recovery/close deadline")
            if runtime.completed(text) and not self.peers:
                self.verify_runtime(runtime, text)
                self.pause(1)
                text = runtime.snapshot(allow_exit=True)
                self.events(poll=True)
                self.verify_runtime(runtime, text)
                connections = [
                    r
                    for r in self.report["boundaries"]
                    if r["boundary"] == "gap" and r["opcode"] == GAP_DEVICE_CONNECTED
                ]
                if len(connections) != 1 or self.peers:
                    raise WatchdogError(
                        "cancellation must close its one exact physical connection"
                    )
                status = self.read_status()
                if not read_completed(status):
                    raise WatchdogError(
                        "cancellation retained receipt drift after disconnect"
                    )
                self.record(
                    "closed",
                    ticket=ticket_document(self.ticket),
                    status=status_document(status),
                    peer_count=0,
                )
                self.report["ledger"] = {
                    "native_commands": ["9027", "9028", "9029"],
                    "verifier": "check_rf_cancellation",
                    "outcome": status.outcome.name,
                }
                return
            self.pause()


if __name__ == "__main__":
    raise SystemExit(main("rf-cancellation"))
