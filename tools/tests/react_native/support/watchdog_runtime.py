"""Synthetic watchdog Central and copied wire receipts; no hardware or live imports."""

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840.cancellation_control import Control
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from tests.peripherals.nrf52840.support.read_fault_emulator import (
    WatchdogWire as Firmware,
)
from tests.support.rf_lab.markers import event, log_parts
from tests.support.rf_lab.markers import marker as raw_marker
from tools.react_native import run_rf_watchdog as runner
from tools.react_native import watchdog_ledger as ledger


def marker(body: str) -> str:
    return raw_marker(body) + "\n"


RUN = "12345678-1234-4234-8234-123456789abc"
CONTROL = Control(RUN, 9, "rf-watchdog", "11111111-1111-4111-8111-111111111111")


class Runtime:
    """Synthetic Central, retaining the same command and barrier transcript."""

    def __init__(
        self, firmware: Firmware, service: str, control: Control = CONTROL
    ) -> None:
        self.firmware = firmware
        self.control = control
        self.signals: list[str] = []
        self.setup, self.gate, self.final = (
            part.replace(ledger.SERVICE, service) for part in log_parts(android=True)
        )
        recovery_at = self.final.index(
            'BLEHUB_NITRO_RUNTIME rf-watchdog:event:{"kind": "scanStarted"'
        )
        self.after_release, self.final = (
            self.final[:recovery_at],
            self.final[recovery_at:],
        )
        self.dispatch = self.setup.splitlines(keepends=True)[-1]
        self.setup = "".join(self.setup.splitlines(keepends=True)[:-1])
        self.text = ""
        self.requests: list[str] = []

    def launch(self) -> None:
        assert not any(
            frame.opcode == fault.Opcode.ARM_READ for frame in self.firmware.commands
        )
        self.firmware.gap(GAP_DEVICE_CONNECTED)
        self.text = marker(self.control.marker("listener", "registered")) + self.setup
        self.text += marker(
            "rf-watchdog:discovery-ready:connectionId=21007:gattSessionId=21009:characteristicId=30"
        )
        self.text += marker(self.control.marker("ready", "arm"))

    def wait_control_request(self, request: str, deadline: float) -> None:
        self.requests.append(request)
        if request == "release":
            self.firmware.clock.now += 16 if self.firmware.fault == "late-gate" else 10
            if self.firmware.fault != "missing-gap":
                self.firmware.gap(GAP_DEVICE_DISCONNECTED)
            self.text += self.gate + marker(self.control.marker("ready", "release"))
        if self.firmware.clock.now >= deadline:
            raise runner.WatchdogError("watchdog control request deadline")
        self.snapshot()
        assert self.control.marker("ready", request) in self.text

    def signal(self, phase: str) -> None:
        self.signals.append(phase)
        self.text += marker(self.control.marker("observed", phase))
        if phase == "armed":
            assert self.firmware.phase is fault.Phase.ARMED and self.firmware.connected
            if self.firmware.fault != "missing-held":
                self.firmware.capture_read()
            self.text += marker(
                "rf-watchdog:active-dispatched:commandId=21010:connectionId=21007:gattSessionId=21009:characteristicId=30"
            )
            self.text += marker(self.control.marker("ready", "read-dispatched"))
        elif phase == "held":
            self.text += self.dispatch
        elif phase == "released":
            assert (
                self.firmware.phase is fault.Phase.RETIRED
                and not self.firmware.connected
            )
            self.text += self.after_release + marker(
                self.control.marker("ready", "recover")
            )
        elif phase == "recovery":
            self.firmware.gap(GAP_DEVICE_CONNECTED)
            self.firmware.gap(GAP_DEVICE_DISCONNECTED)
            self.text += self.final
            if self.firmware.fault == "late-route":
                self.text += event(
                    "characteristicRead",
                    "21010",
                    connectionId="21007",
                    gattSessionId="21009",
                    characteristicId="30",
                    value=[0],
                )

    def snapshot(self, *, allow_exit: bool = False) -> str:
        ledger.verify_control(self.text, self.control)
        return self.text
