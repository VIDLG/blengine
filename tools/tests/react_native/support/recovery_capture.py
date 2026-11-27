"""Synthetic SDK receipts, stream children and clock for recovery capture tests."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.peripherals.nrf52840 import recovery_ten as protocol
from tools.react_native import capture_io, capture_transport
from tools.react_native import recovery_ten_capture as api
from tools.react_native.host_contract import (
    ANDROID_PACKAGE,
    APP_NAME,
    BUNDLE_ID,
    SCHEME,
)

RUN = "12345678-1234-4234-8234-123456789abc"

BOOT = "22222222-2222-4222-8222-222222222222"

SERVICE = "fff00000-0000-1000-8000-00805f9b34fb"

DEVICE = "physical-device"

DEVICE_ID = "official-device-identifier"

IOS_URL = f"file:///private/var/containers/Bundle/Application/install/{APP_NAME}/"

ANDROID_URI = "file:/data/app/~~install==/com.blehubnitrohost-code==/base.apk"


def document(command: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "info": {
            "outcome": "success",
            "commandType": command,
            "arguments": [*command.split("."), "--device", DEVICE],
        },
        "result": {"deviceIdentifier": DEVICE_ID, **result},
    }


def resources(phase: str, android: bool) -> dict[str, int]:
    counts = dict.fromkeys(protocol.RESOURCE_FIELDS, 0)
    if phase != "closed":
        counts.update(
            dict.fromkeys(
                (
                    "hubRoots",
                    "handoffRoots",
                    "ingressRoots",
                    "backendLeases",
                    "adapterDelegates",
                    "listeners",
                ),
                1,
            )
        )
        counts.update(
            workerThreads=2 if android else 0, lifecycleOwners=1 if android else 0
        )
    if phase == "active":
        counts.update(
            connections=1, subscriptions=1, gattAttributes=3, callbackRoutes=1
        )
    return counts


def cycle_markers(cycle: int, android: bool, *, before: bool) -> list[str]:
    """Synthetic markers accepted by the actual provider RuntimeFacts parser."""
    b = 10000 + 100 * cycle
    steps = [
        ("initialize", f"commandId={b}"),
        (
            "scan",
            f"scanSessionId={b + 1}:startCommandId={b + 2}:stopCommandId={b + 3}:peripheralId=1:serviceUuid={SERVICE}:silenceMs=2000",
        ),
        ("connected", f"commandId={b + 5}:connectionId={b + 4}:peripheralId=1"),
        (
            "gatt",
            f"commandId={b + 7}:connectionId={b + 4}:gattSessionId={b + 6}:serviceCount=1:characteristicCount=1:descriptorCount=1",
        ),
        (
            "notification-enable",
            f"commandId={b + 9}:connectionId={b + 4}:gattSessionId={b + 6}:subscriptionId={b + 8}:characteristicId=1:actualMode=Notification",
        ),
        (
            "notification-value",
            f"connectionId={b + 4}:gattSessionId={b + 6}:subscriptionId={b + 8}:value=ef",
        ),
        (
            "passive-disconnect",
            f"commandId=none:connectionId={b + 4}:error={'connection' if android else 'none'}",
        ),
        (
            "old-route-silence",
            f"connectionId={b + 4}:gattSessionId={b + 6}:subscriptionId={b + 8}:observationMs=1000",
        ),
        (
            "recovery-connected",
            f"commandId={b + 11}:connectionId={b + 10}:peripheralId=1",
        ),
        (
            "recovery-gatt",
            f"commandId={b + 13}:connectionId={b + 10}:gattSessionId={b + 14}:serviceCount=1:characteristicCount=1:descriptorCount=1",
        ),
        (
            "recovery-read",
            f"commandId={b + 15}:connectionId={b + 10}:gattSessionId={b + 14}:characteristicId=1:value=00",
        ),
        (
            "recovery-disconnected",
            f"commandId={b + 12}:connectionId={b + 10}:observationMs=1000",
        ),
        (
            "cycle",
            f"scanSessionId={b + 1}:connectionId={b + 4}:recoveryConnectionId={b + 10}",
        ),
    ]
    mapped = {name: f"cycle:{cycle}:{name}:passed:{fields}" for name, fields in steps}
    passive = {
        "kind": "connectionStateChanged",
        "connectionId": str(b + 4),
        "runId": RUN,
        "nonce": "same-js-boot",
        "cycle": cycle,
        **(
            {"error": {"category": "connection", "message": "synthetic device loss"}}
            if android
            else {"connectionState": "Disconnected"}
        ),
    }
    mapped["passive-disconnect"] = (
        "event:" + json.dumps(passive) + "\n" + mapped["passive-disconnect"]
    )
    for phase in protocol.RESOURCE_PHASES:
        mapped[f"resource-{phase}"] = (
            f"resources:cycle={cycle}:phase={phase}:snapshot={json.dumps(resources(phase, android))}"
        )
    names = (
        (
            "initialize",
            "resource-baseline",
            "scan",
            "connected",
            "gatt",
            "notification-enable",
            "resource-active",
            "notification-value",
            "passive-disconnect",
            "old-route-silence",
            "resource-passive",
        )
        if before
        else (
            "recovery-connected",
            "recovery-gatt",
            "recovery-read",
            "recovery-disconnected",
            "resource-recovery-disconnected",
            "resource-closed",
            "cycle",
        )
    )
    if before:
        if android:
            timestamp = 30000 + cycle * 7000
            mapped["resource-baseline"] += (
                f"\ncycle:{cycle}:scan-budget-wait:waitMs={30000 if cycle == 0 else 0}:"
                f"monotonicMs={0 if cycle == 0 else timestamp}:"
                "policy=android-conservative-test-only-v1:quietMs=30000:minStartIntervalMs=7000"
                f"\ncycle:{cycle}:scan-budget-dispatch:monotonicMs={timestamp}"
                f"\ncycle:{cycle}:scan-budget-started:monotonicMs={timestamp}"
            )
        return [
            *([protocol.SCAN_BUDGET_POLICY] if android and cycle == 0 else []),
            f"cycle:{cycle}:start",
            "listener:registered-before-initialize",
            *[part for name in names for part in mapped[name].splitlines()],
            f"cycle:{cycle}:recovery-awaiting:runId={RUN}",
        ]
    return [*[mapped[n] for n in names], f"cycle:{cycle}:passed"]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Child:
    """Owned local SDK/console child; commands can emit ACKs before returning."""

    def __init__(self, harness: Harness, command: list[str], output: Any) -> None:
        self.harness, self.command, self.output = harness, command, output
        self.pid = 8000 + len(harness.children)
        self.returncode: int | None = None
        self.console = "--console" in command or "logcat" in command
        self.ready = harness.clock.now + (
            0.4 if "--payload-url" in command or "-d" in command else 0.1
        )

    def poll(self) -> int | None:
        if (
            not self.console
            and self.returncode is None
            and self.harness.clock.now >= self.ready
        ):
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        if self.console and self.harness.platform == "ios":
            raise AssertionError("catchable signal forwarded to iOS app")
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


class Harness:
    def __init__(self, root: Path, platform: str = "ios", fault: str = "") -> None:
        self.root, self.platform, self.fault = root, platform, fault
        self.log = root / "capture.log"
        self.coordination = root / "coordination"
        self.coordination.mkdir()
        self.install = root / "install.json"
        api.write_document(
            self.install,
            document(
                "devicectl.device.install.app",
                {
                    "installedApplications": [
                        {"bundleID": BUNDLE_ID, "installationURL": IOS_URL}
                    ]
                },
            ),
        )
        self.clock = Clock()
        self.children: list[Child] = []
        self.capture: api._Capture | None = None
        self.console: Child | None = None
        self.controls: list[str] = []
        self.guard_calls = 0
        self.published_during_relay = False
        self.identity = {
            "run_id": RUN,
            "pid": 42,
            "boot_id": BOOT,
            "bundle_id": ANDROID_PACKAGE if platform == "android" else BUNDLE_ID,
            "executable": ANDROID_URI if platform == "android" else IOS_URL + SCHEME,
            "source": "launch",
        }

    def append(self, markers: list[str]) -> None:
        assert self.console is not None
        for marker in markers:
            prefix = (
                "09-27 12:34:56.789    42    43 I ReactNativeJS: "
                if self.platform == "android"
                else ""
            )
            self.console.output.write((prefix + api.PREFIX + marker + "\n").encode())
        self.console.output.flush()

    def progress(self, cycle: int, *, closed: bool = False) -> None:
        api.write_document(
            self.coordination / api.PROGRESS_NAME,
            {
                "schema_version": 1,
                "mode": api.MODE,
                "run_id": RUN,
                "cycle": cycle,
                "phase": "closed" if closed else "recovery-ready",
                "status": "passed" if closed else "running",
                "physical_closed": closed,
                "recovery_url": None
                if closed
                else f"blehub-rn-spike://recover/{RUN}/{cycle}",
            },
        )

    def guard(self) -> None:
        self.guard_calls += 1
        if self.fault == "guard" and len(self.controls) == 1:
            raise RuntimeError("fixture receipt replaced")

    def initial_pid(self, device: str, log: Path, budget: float, *, guard: Any) -> str:
        guard()
        api.write_document(
            log.with_suffix(".identity.json"),
            {"status": "pinned", "observations": [{"output": "42"}]},
        )
        return "42"

    def command_timing(self, child: Child, command: list[str]) -> None:
        if self.fault == "command-timeout" and "processes" in command:
            child.ready = self.clock.now + 3
        if self.fault == "post-budget":
            if "--payload-url" in command:
                child.ready = self.clock.now + 1.4
            elif "processes" in command and self.controls:
                child.ready = self.clock.now + 0.8

    def popen(self, command: list[str], **kwargs: Any) -> Child:
        child = Child(self, command, kwargs["stdout"])
        self.children.append(child)
        self.command_timing(child, command)
        if child.console:
            self.console = child
            self.append(
                [
                    "native:bootstrap:" + json.dumps(self.identity),
                    f"runtime:runId={RUN}:nonce=same-js-boot:phase=start",
                    *cycle_markers(0, self.platform == "android", before=True),
                ]
            )
            if self.fault == "early":
                self.append(
                    [
                        "native:signal:"
                        + json.dumps(
                            {
                                **self.identity,
                                "cycle": 0,
                                "source": "payload-url",
                                "url": f"blehub-rn-spike://recover/{RUN}/0",
                            }
                        )
                    ]
                )
            self.progress(0)
        elif "pm" in command:
            child.output.write(
                ("package:" + ANDROID_URI.removeprefix("file:") + "\n").encode()
            )
        elif "pidof" in command:
            child.output.write(
                b"43\n" if self.fault == "replacement" and self.controls else b"42\n"
            )
        elif "processes" in command:
            path = Path(command[command.index("--json-output") + 1])
            executable = self.identity["executable"]
            if self.fault == "replacement" and self.controls:
                executable = "file:///replacement"
            api.write_document(
                path,
                document(
                    "devicectl.device.info.processes",
                    {
                        "runningProcesses": [
                            {"processIdentifier": 42, "executable": executable}
                        ]
                    },
                ),
            )
        elif "--payload-url" in command or "android.intent.action.VIEW" in command:
            key = "--payload-url" if self.platform == "ios" else "-d"
            url = command[command.index(key) + 1]
            self.controls.append(url)
            cycle = len(self.controls) - 1
            record = {
                **self.identity,
                "source": "payload-url",
                "cycle": cycle,
                "url": url,
            }
            if self.fault == "foreign":
                record["boot_id"] = RUN
            native = "native:signal:" + json.dumps(record)
            js = f"cycle:{cycle}:recovery-ready:runId={RUN}:source=linking"
            if self.fault != "expired":
                self.append([js, native] if self.fault == "js-first" else [native, js])
            if self.fault == "duplicate":
                self.append([native])
            if self.fault == "native-failed":
                self.append(["native:failed:foreign-or-duplicate-payload-url"])
            if self.fault != "expired":
                self.append(
                    cycle_markers(cycle, self.platform == "android", before=False)
                )
                if cycle < 9:
                    self.append(
                        cycle_markers(
                            cycle + 1, self.platform == "android", before=True
                        )
                    )
                    self.progress(cycle + 1)
                else:
                    self.append(
                        [
                            "close:passed:listener-removed=true:probe-closed=true",
                            f"runtime:runId={RUN}:nonce=same-js-boot:phase=close",
                            f"passed:cycles=10:runId={RUN}",
                        ]
                    )
                    self.progress(9, closed=True)
            if self.platform == "ios":
                api.write_document(
                    Path(command[command.index("--json-output") + 1]),
                    self.launch_receipt(command, url),
                )
        child.output.flush()
        return child

    def launch_receipt(self, command: list[str], url: str) -> dict[str, Any]:
        receipt = document(
            "devicectl.device.process.launch",
            {
                "process": {
                    "processIdentifier": 42,
                    "executable": self.identity["executable"],
                }
            },
        )
        receipt["info"]["arguments"] = command[1:]
        return receipt

    def sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)
        if self.controls:
            raw = self.coordination / "runtime.log"
            text = raw.read_text() if raw.exists() else ""
            if ":recovery-ready:runId=" in text and any(
                "--payload-url" in c.command and c.poll() is None for c in self.children
            ):
                self.published_during_relay = True
            if (
                f"passed:cycles=10:runId={RUN}" in text
                and not (self.coordination / api.PHYSICAL_NAME).exists()
            ):
                assert self.capture is not None
                api.write_document(
                    self.coordination / api.PHYSICAL_NAME,
                    {
                        "mode": api.MODE,
                        "run_id": RUN,
                        "service_uuid": SERVICE,
                        "status": "failed"
                        if self.fault == "physical-failed"
                        else "passed",
                        "native_bootstrap": self.capture.facts.bootstrap,
                        "native_signals": self.capture.facts.signals,
                    },
                )

    def killpg(self, pid: int, sig: int) -> None:
        assert sig == signal.SIGKILL
        child = next(c for c in self.children if c.pid == pid)
        child.kill()

    def run(self) -> tuple[str, tuple[int, ...]]:
        with (
            patch.object(api.time, "monotonic", self.clock.monotonic),
            patch.object(api.time, "sleep", self.sleep),
            patch.object(api.subprocess, "Popen", self.popen),
            patch.object(capture_io.os, "killpg", self.killpg),
            patch.object(
                capture_transport.android_sdk,
                "adb_command",
                lambda device, *args: ["adb", "-s", device, *args],
            ),
            patch.object(capture_transport, "wait_for_initial_pid", self.initial_pid),
        ):
            self.capture = api._Capture(
                self.platform,
                DEVICE,
                self.log,
                self.coordination,
                RUN,
                SERVICE,
                "fixture-name",
                900,
                self.guard,
                self.install if self.platform == "ios" else None,
            )
            return self.capture.run()
