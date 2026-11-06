"""Real provider writer + capture pump over synthetic SDK children and typed BTP.

The provider runs on its own thread and publishes its own evidence. Phone data
is simulated; no prewritten successful write-reject report authorizes a signal.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840 import write_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    write_document,
)
from tests.peripherals.nrf52840.support.write_reject_emulator import (
    Harness as Peripheral,
)
from tools.react_native import capture_io, capture_transport
from tools.react_native import write_reject_capture as api
from tools.react_native.host_contract import ANDROID_PACKAGE
from tools.tests.react_native.support.ios_receipts import (
    ios_bootstrap,
    ios_process_document,
    write_ios_install,
)
from tools.tests.react_native.support.recovery_capture import ANDROID_URI, Child


class Clock:
    @property
    def now(self) -> float:
        return time.monotonic()


class Phone(Peripheral):
    def ready(self, request: str) -> None:
        self.requests.append(request)
        self.lines.append(self.control.marker("ready", request))


class Replay:
    protocol = protocol
    scenario = "write-reject"
    capture_type = api.WriteRejectCapture

    def _new_phone(self, root, platform):
        return Phone(root, platform)

    def _new_owner(self):
        return protocol.WriteReject(self.phone.fixture, self.root, {"read-write": 51})

    def __init__(self, root: Path, platform: str = "android", fault: str = "") -> None:
        self.phone = self._new_phone(root, platform)
        if fault == "native-close":
            self.phone.fault = "native-close"
        if fault == "provider-held":
            self.phone.wire.fault = "wrong-request"
        self.root, self.platform, self.fault = self.phone.root, platform, fault
        self.log = root / f"{self.scenario}-runtime.log"
        self.install = root / "device-install.json"
        write_ios_install(self.install)
        self.clock = Clock()
        self.phone.owner = self._new_owner()
        self.capture = None
        self.children = []
        self.console = None
        self.controls = []
        self.provider_errors = []
        self.cursor = 0
        self.guard_calls = 0
        self.published_during_sdk = False
        self.provider_closed_after_app = False
        self.identity = ios_bootstrap(self.phone.control)
        if platform == "android":
            self.identity.update(bundle_id=ANDROID_PACKAGE, executable=ANDROID_URI)
        self.thread = threading.Thread(target=self.provider, daemon=True)

    def provider(self) -> None:
        try:
            self.phone.owner.run()
            self.provider_closed_after_app = (
                f"rf-{self.scenario}:passed" in (self.root / "runtime.log").read_text()
            )
        except BaseException as error:  # noqa: BLE001 - transport the provider thread failure to its test owner
            self.provider_errors.append(error)

    def guard(self) -> None:
        self.guard_calls += 1
        if self.fault == "lease" and self.controls:
            raise RuntimeError("synthetic fixture lease lost")

    def flush(self) -> None:
        assert self.console is not None
        for body in self.phone.lines[self.cursor :]:
            if self.fault == "wrong-error":
                body = body.replace('"code": "14"', '"code": "133"')
            if self.fault == "nested-error":
                body = body.replace('"ATT error"', '"ATT:failed:expected native error"')
            header = (
                "10-19 12:34:56.789    42    43 I ReactNativeJS: "
                if self.platform == "android"
                else ""
            )
            self.console.output.write(
                (header + self.protocol.PREFIX + body + "\n").encode()
            )
        self.console.output.flush()
        self.cursor = len(self.phone.lines)

    def initial_pid(self, device, log, budget, *, guard):
        guard()
        write_document(
            log.with_suffix(".identity.json"),
            {"status": "pinned", "observations": [{"output": "42"}]},
        )
        return "42"

    def signal(self, phase: str) -> None:
        record = {**self.identity, "phase": phase}
        if self.fault == "token":
            record["token"] = self.phone.control.session
        if self.fault == "boot":
            record["boot_id"] = self.phone.control.session
        native = "rf-host:signal:" + json.dumps(record)
        if self.fault == "ack-timeout":
            return
        if self.fault == "js-first":
            self.phone.lines.append(self.phone.control.marker("observed", phase))
        self.phone.lines.append(native)
        if self.fault == "duplicate":
            self.phone.lines.append(native)
        # Reuse the Peripheral sample's phone executor, not its relay. The actual
        # capture's production relay has already consumed the matching signal.
        self.phone.relay = SimpleNamespace(
            pending=lambda: phase, forwarded=lambda p: None, observe=lambda text: None
        )
        self.phone.started = True
        self.phone.tick(0)
        self.flush()

    def popen(self, command, **kwargs):
        child = Child(self, command, kwargs["stdout"])
        self.children.append(child)
        # SDK delay guarantees that both ACKs can precede command return.
        child.ready = self.clock.now + (
            0.08
            if "notification" in command or "android.intent.action.VIEW" in command
            else 0.005
        )
        if self.fault == "sdk-timeout" and "android.intent.action.VIEW" in command:
            child.ready = self.clock.now + 3
        if self.fault == "post-budget":
            if "notification" in command or "android.intent.action.VIEW" in command:
                child.ready = self.clock.now + 1.3
            elif self.controls and ("processes" in command or "pidof" in command):
                child.ready = self.clock.now + 0.8
        if child.console:
            self.console = child
            self.phone.lines.append("rf-host:bootstrap:" + json.dumps(self.identity))
            self.phone.start_app()
            if self.fault == "early":
                self.phone.lines.append(
                    "rf-host:signal:" + json.dumps({**self.identity, "phase": "armed"})
                )
            self.flush()
        elif "pm" in command:
            child.output.write(
                ("package:" + ANDROID_URI.removeprefix("file:") + "\n").encode()
            )
        elif "pidof" in command:
            child.output.write(
                b"43\n" if self.fault == "pid" and self.controls else b"42\n"
            )
        elif "processes" in command:
            path = Path(command[command.index("--json-output") + 1])
            value = ios_process_document()
            if self.fault == "pid" and self.controls:
                value["result"]["runningProcesses"][0]["processIdentifier"] = 43
            write_document(path, value)
        elif (
            "notification" in command
            and "post" in command
            or "android.intent.action.VIEW" in command
        ):
            phase = self.protocol.PHASES[len(self.controls)]
            self.controls.append(phase)
            if self.platform == "ios":
                path = Path(command[command.index("--json-output") + 1])
                value = ios_process_document()
                value["info"].update(
                    commandType="devicectl.device.notification.post",
                    arguments=command[1:],
                )
                value["result"].pop("runningProcesses")
                if self.fault == "sdk-receipt":
                    value["info"]["arguments"] = [
                        "devicectl",
                        "device",
                        "notification",
                        "post",
                        "--device",
                        "device",
                        "--name",
                        "foreign",
                    ]
                write_document(path, value)
            self.signal(phase)
            if self.fault == "sdk-reject":
                child.returncode = 1
                child.output.write(b"synthetic SDK rejection\n")
        child.output.flush()
        return child

    def killpg(self, pid, sig):
        assert sig == signal.SIGKILL
        next(c for c in self.children if c.pid == pid).kill()

    def make_capture(self, timeout=8):
        return self.capture_type(
            self.platform,
            "device",
            self.log,
            self.root,
            self.phone.plan.session_id,
            self.phone.plan.service_uuid,
            self.phone.fixture._profile.local_name,
            timeout,
            self.guard,
            self.install if self.platform == "ios" else None,
            fixture_identity=(os.getpid(), os.getpgid(0)),
        )

    def run(self):
        self.thread.start()
        deadline = time.monotonic() + 2
        while not (self.root / self.protocol.PHYSICAL_NAME).exists() or not any(
            r["boundary"] == "no-peer"
            for r in fixture_session.read_live_document(
                self.root / self.protocol.PHYSICAL_NAME
            )["boundaries"]
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("synthetic provider startup deadline")
            time.sleep(0.001)
        try:
            with (
                patch.object(capture_transport.subprocess, "Popen", self.popen),
                patch.object(
                    capture_transport.android_sdk,
                    "adb_command",
                    lambda device, *args: ["adb", "-s", device, *args],
                ),
                patch.object(
                    capture_transport, "wait_for_initial_pid", self.initial_pid
                ),
                patch.object(capture_io.os, "killpg", self.killpg),
            ):
                self.capture = self.make_capture(timeout=8)
                original_poll = self.capture._poll

                def poll():
                    original_poll()
                    if self.capture.in_relay and self.capture.js_acknowledged:
                        self.published_during_sdk = True
                        if len(self.controls) < len(self.protocol.PHASES):
                            next_request = (
                                self.root
                                / f"request-{self.protocol.REQUESTS[len(self.controls)]}.json"
                            )
                            assert not next_request.exists(), (
                                "provider request escaped before SDK return"
                            )
                    if (
                        self.fault == "raw-drift"
                        and self.controls
                        and self.console is not None
                    ):
                        self.log.with_suffix(".raw.log").write_bytes(
                            b"rewritten capture\n"
                        )

                self.capture._poll = poll
                return self.capture.run()
        finally:
            self.phone.fixture._stop_requested = True
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise AssertionError("synthetic provider thread was not reaped")
