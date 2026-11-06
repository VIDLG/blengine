"""One-child SDK replay through the actual shared pump, not hardware/RF proof."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import cancellation_control as control_protocol
from tests.peripherals.nrf52840 import recovery_ten as protocol
from tests.peripherals.nrf52840.cancellation_control import write_document
from tools.react_native import capture_io
from tools.react_native import capture_transport as shared
from tools.react_native import full_suite_capture as api
from tools.tests.react_native.support.full_suite_ledger import (
    SuiteFixture,
    replay_physical_pending,
)
from tools.tests.react_native.support.recovery_capture import Child, Clock, Harness


class Replay(Harness):
    def __init__(self, root: Path, platform: str, fault: str = "") -> None:
        self.fixture = SuiteFixture(root, platform)
        self.root, self.platform, self.fault = root, platform, fault
        self.log = root / "captured-runtime.log"
        (root / "recovery-ten-runtime.log").unlink()
        self.coordination = self.fixture.root
        self.install = root / "device-install.json"
        self.identity = self.fixture.boot
        self.clock = Clock()
        self.clock.now = time.monotonic()
        self.children = []
        self.capture = self.console = None
        self.controls = []
        self.guard_calls = 0
        self.published_during_relay = False
        self.queue = self.fixture.text.splitlines()
        self.cursor = 0
        self.raw_seen: list[str] = []
        for path in (
            self.coordination / "runtime.log",
            self.coordination / "recovery" / "runtime.log",
            self.coordination / "recovery" / protocol.PROGRESS_NAME,
        ):
            path.unlink(missing_ok=True)
        for control in self.fixture.controls:
            directory = self.coordination / control.directory
            for phase in control.phases:
                write_document(directory / f"signal-{phase}.json", asdict(control))

    def drain(self, *, signal: bool = False) -> None:
        assert self.console is not None
        if signal:
            line = self.queue[self.cursor]
            if self.fault == "control-foreign" and "rf-host:signal:" in line:
                line = line.replace(
                    self.identity["boot_id"], self.fixture.plan.session_id
                )
            if self.fault == "begin-foreign" and "rf-lab-full:native:signal:" in line:
                line = line.replace(
                    self.identity["boot_id"], self.fixture.plan.session_id
                )
            if self.fault == "begin-expired" and "rf-lab-full:native:signal:" in line:
                self.clock.now += 2
            if self.fault == "begin-js-first" and "rf-lab-full:native:signal:" in line:
                self.console.output.write((self.queue[self.cursor + 1] + "\n").encode())
            self.console.output.write((line + "\n").encode())
            if self.fault == "begin-duplicate" and "rf-lab-full:native:signal:" in line:
                self.console.output.write((line + "\n").encode())
            self.raw_seen.append(line)
            self.cursor += 1
        while self.cursor < len(self.queue):
            line = self.queue[self.cursor]
            if any(
                p in line
                for p in (
                    "rf-host:signal:",
                    "rf-lab-full:native:signal:",
                    "rf-recovery-ten:native:signal:",
                )
            ):
                break
            if self.fault == "nonce" and "rf-recovery-ten:runtime:" in line:
                line = line.replace("synthetic-one-runtime", "restarted-runtime")
            self.console.output.write((line + "\n").encode())
            self.raw_seen.append(line)
            self.cursor += 1
            if "rf-recovery-ten:cycle:" in line and ":recovery-awaiting:" in line:
                cycle = int(line.split("rf-recovery-ten:cycle:")[1].split(":")[0])
                write_document(
                    self.coordination / "recovery" / protocol.PROGRESS_NAME,
                    {
                        "schema_version": 1,
                        "mode": protocol.MODE,
                        "run_id": self.fixture.plan.session_id,
                        "cycle": cycle,
                        "phase": "recovery-ready",
                        "status": "running",
                        "recovery_url": f"blehub-rn-spike://recover/{self.fixture.plan.session_id}/{cycle}",
                    },
                )
            if "rf-recovery-ten:passed:" in line:
                write_document(
                    self.coordination / "recovery" / protocol.PROGRESS_NAME,
                    {
                        "schema_version": 1,
                        "mode": protocol.MODE,
                        "run_id": self.fixture.plan.session_id,
                        "cycle": 9,
                        "phase": "closed",
                        "status": "passed",
                        "physical_closed": True,
                    },
                )
        self.console.output.flush()

    def popen(self, command: list[str], **kwargs) -> Child:
        console = "--console" in command or "logcat" in command
        relay = (
            "--payload-url" in command
            or "android.intent.action.VIEW" in command
            or "notification" in command
        )
        if not console and not relay:
            return super().popen(command, **kwargs)
        child = Child(self, command, kwargs["stdout"])
        self.children.append(child)
        if console:
            self.console = child
            self.drain()
        else:
            self.controls.append(command)
            if self.fault == "replacement" and len(self.controls) == 8:
                self.identity = {**self.identity, "executable": "file:///replacement"}
            if "--payload-url" in command:
                url = command[command.index("--payload-url") + 1]
                write_document(
                    Path(command[command.index("--json-output") + 1]),
                    self.launch_receipt(command, url),
                )
            if (
                self.fault == "handoff-drift"
                and "rf-lab-full:native:signal:" in self.queue[self.cursor]
            ):
                self.fixture.handoff["peer_count"] = 1
                write_document(
                    self.coordination / "full-suite-handoff.json", self.fixture.handoff
                )
            if not (
                self.fault == "begin-noack"
                and "rf-lab-full:native:signal:" in self.queue[self.cursor]
            ):
                self.drain(signal=True)
        return child

    def sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)
        if self.capture is not None and self.controls:
            text = (self.coordination / "runtime.log").read_text(encoding="utf-8")
            if ":recovery-ready:" in text and any(
                c.returncode is None and not c.console for c in self.children
            ):
                self.published_during_relay = True

    def run(self):
        with (
            patch.object(
                control_protocol.ControlRelay, "pending", new=replay_physical_pending
            ),
            patch.object(shared.time, "monotonic", self.clock.monotonic),
            patch.object(shared.time, "sleep", self.sleep),
            patch.object(shared.subprocess, "Popen", self.popen),
            patch.object(capture_io.os, "killpg", self.killpg),
            patch.object(
                shared.android_sdk,
                "adb_command",
                lambda device, *args: ["adb", "-s", device, *args],
            ),
            patch.object(shared, "wait_for_initial_pid", self.initial_pid),
        ):
            self.capture = api._Capture(
                self.platform,
                "physical-device",
                self.log,
                self.coordination,
                self.fixture.plan.session_id,
                self.fixture.plan.service_uuid,
                self.fixture.h.fixture._profile.local_name,
                120,
                self.guard,
                self.install if self.platform == "ios" else None,
                fixture_identity=self.fixture.identity,
            )
            return self.capture.run()


class FullSuiteCaptureTests(unittest.TestCase):
    def make(self, platform: str, fault: str = "") -> Replay:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Replay(Path(temporary.name), platform, fault)

    def test_one_capture_one_boot_hard_ack_pump_both_platforms(self) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                text, cycles = h.run()
                self.assertEqual(cycles, tuple(range(10)))
                self.assertEqual(len([c for c in h.children if c.console]), 1)
                self.assertEqual(len(h.controls), 18)
                self.assertEqual(text.splitlines(), h.raw_seen)
                self.assertTrue(h.published_during_relay)
                h.fixture.log = h.log
                h.fixture.text = text
                result = h.fixture.verify()
                self.assertEqual(result["counts"]["recovery_terminals"], 100)
                launch = (
                    next(c.command for c in h.children if c.console)
                    if platform == "ios"
                    else next(
                        c.command
                        for c in h.children
                        if "blehub.runtimeMode" in c.command
                    )
                )
                self.assertIn(
                    "--blehub-nitro-rf-cancellation-control"
                    if platform == "ios"
                    else "blehub.rfCancellationControl",
                    launch,
                )
                self.assertNotIn("--blehub-nitro-rf-recovery-ten-service", launch)

    def test_base_ui_pass_cannot_authorize_begin_without_original_gates(self) -> None:
        for platform in ("ios", "android"):
            for fault in ("missing-readback", "physical-failed", "handoff-peer"):
                with self.subTest(platform=platform, fault=fault):
                    h = self.make(platform)
                    if fault == "missing-readback":
                        h.queue = [
                            l
                            for l in h.queue
                            if "rf:step:read-response:passed:" not in l
                        ]
                    elif fault == "physical-failed":
                        path = (
                            h.coordination
                            / "flow-9-rf-watchdog"
                            / "physical-evidence.json"
                        )
                        value = shared.read_document(path)
                        value["status"] = "failed"
                        write_document(path, value)
                    else:
                        h.fixture.handoff["peer_count"] = 1
                        write_document(
                            h.coordination / "full-suite-handoff.json",
                            h.fixture.handoff,
                        )
                    with self.assertRaises((RuntimeError, AssertionError, ValueError)):
                        h.run()
                    if fault == "physical-failed":
                        self.assertLess(
                            len(h.controls), 7
                        )  # Failed evidence blocks even its next barrier.
                    else:
                        self.assertEqual(len(h.controls), 7)
                    self.assertIsNone(h.capture.begin)
                    self.assertEqual(h.capture.actions, [])

    def test_begin_identity_deadline_restart_and_nonce_fail_closed(self) -> None:
        for platform in ("ios", "android"):
            for fault in (
                "begin-foreign",
                "begin-expired",
                "nonce",
                "control-foreign",
                "begin-js-first",
                "begin-duplicate",
                "begin-noack",
                "handoff-drift",
                "replacement",
            ):
                with self.subTest(platform=platform, fault=fault):
                    h = self.make(platform, fault)
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        h.run()
                    self.assertEqual(h.capture.receipt["status"], "failed")
                    self.assertEqual(len([c for c in h.children if c.console]), 1)
                    self.assertTrue(all(c.returncode is not None for c in h.children))


if __name__ == "__main__":
    unittest.main()
