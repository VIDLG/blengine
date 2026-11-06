"""Consistent synthetic full-suite runtime, capture and owner documents."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import cancellation_control as control_protocol
from tests.peripherals.nrf52840 import full_suite as protocol
from tests.peripherals.nrf52840 import recovery_ten
from tests.peripherals.nrf52840.cancellation_control import (
    load_suite_controls,
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.support.recovery_emulator import HANDLES
from tests.peripherals.nrf52840.support.watchdog_reports import fake_watchdog
from tests.support.rf_lab.markers import (
    CANCELLATION_MARKERS,
    GATT_MARKERS,
    IO_MARKERS,
    MARKERS,
    controlled_markers,
)
from tools.react_native import full_suite_ledger as api
from tools.react_native.host_contract import SCHEME
from tools.tests.react_native.support import runtime_markers as base
from tools.tests.react_native.support.recovery_capture import (
    ANDROID_URI,
    IOS_URL,
    document,
)
from tools.tests.react_native.support.recovery_ledger import (
    SyntheticHarness,
    synthetic_events,
)

_live_pending = control_protocol.ControlRelay.pending


def replay_physical_pending(relay):
    """Replay each copied v2 physical boundary at its own synthetic instant.

    Capture/SDK replay runs faster than the 10s native watchdog. It must not
    reuse a single wall-clock instant for ARM and post-watchdog retirement.
    Independent controller tests enforce the real absolute deadlines.
    """
    evidence = read_document(relay.directory / "physical-evidence.json")
    index = len(relay.sent)
    if index == len(relay.control.phases):
        return _live_pending(relay)
    phase = relay.control.phases[index]
    name = {
        "armed": "armed",
        "held": "held",
        "released": "retired-after-watchdog"
        if relay.control.flow == 9
        else "read-completed",
        "recovery": "post-retirement-old-route-silence",
    }[phase]
    instant = (
        next(b["monotonic"] for b in evidence["boundaries"] if b["boundary"] == name)
        + 0.001
    )
    with patch.object(control_protocol.time, "monotonic", return_value=instant):
        return _live_pending(relay)


class RecoveryHarness(SyntheticHarness):
    def identity(self) -> dict:
        return {
            **super().identity(),
            "executable": ANDROID_URI
            if self.platform == "android"
            else IOS_URL + SCHEME,
        }


class SuiteFixture:
    """Current schemas with simulated BTP recovery, not native or RF proof."""

    def __init__(self, directory: Path, platform: str = "android") -> None:
        self.h = RecoveryHarness(directory, platform, "")
        self.plan = self.h.plan
        self.root = self.plan.root.parent
        self.platform = platform
        protocol.prepare_control(self.root, self.plan)
        self.h.root = self.root / "recovery"
        self.h.clock.now = 102.0  # Provider begin authorization below is at 101.
        self.h.retain_watchdog()
        self.recovery = self.h.run()
        self.identity = (os.getpid(), os.getpgid(0))
        self.log = directory / "full-suite-runtime.log"
        self.boot = self.h.identity()
        self.controls = load_suite_controls(self.root)
        self.control_boot = {
            **asdict(self.controls[0]),
            **{k: self.boot[k] for k in ("pid", "boot_id", "bundle_id", "executable")},
        }
        cancellation_path = (
            self.root / self.controls[0].directory / "physical-evidence.json"
        )
        attempt = SimpleNamespace(
            root=self.root,
            plan=self.plan,
            path=cancellation_path,
            evidence=read_document(cancellation_path),
            control=self.controls[0],
        )
        self.watchdog = fake_watchdog(attempt, anchor=75.0)
        for item in self.watchdog.evidence["canonical_values"]:
            item["handle"] = HANDLES[item["role"]]
        write_document(self.watchdog.path, self.watchdog.evidence)
        run, nonce = self.plan.session_id, "synthetic-one-runtime"
        self.begin = {
            "url": api.begin_url(run),
            "requested_at": 100.0,
            "native_ack_at": 100.1,
            "js_ack_at": 100.2,
            "native_signal": {
                **self.boot,
                "source": "payload-url",
                "url": api.begin_url(run),
            },
        }
        armed = next(
            b for b in self.watchdog.evidence["boundaries"] if b["boundary"] == "armed"
        )
        retired = next(
            b
            for b in self.watchdog.evidence["boundaries"]
            if b["boundary"] == "retired-after-watchdog"
        )
        fault_ticket = {k: armed[k] for k in ("wire_hex", "sequence", "handle")}
        self.handoff = {
            "schema_version": 1,
            "run_id": run,
            "status": "ready",
            "fixture_identity": {"pid": self.identity[0], "pgid": self.identity[1]},
            "hold_inactive": True,
            "peer_count": 0,
            "advertising_stop_confirmed": True,
            "canonical_values_restored": True,
            "recovery_url": api.begin_url(run),
            "fault_ticket": fault_ticket,
            "canonical_values": copy.deepcopy(
                self.watchdog.evidence["canonical_values"]
            ),
            "boundaries": [
                {
                    "boundary": "base-physical-closed",
                    "monotonic": 90.0,
                    "fault_ticket": fault_ticket,
                },
                {
                    "boundary": "fresh-idle",
                    "monotonic": 91.0,
                    "fault_ticket": fault_ticket,
                    "status_wire": retired["status_wire"],
                    "peer_count": 0,
                },
            ],
        }
        write_document(self.root / protocol.HANDOFF_NAME, self.handoff)
        self.physical = {
            "schema_version": 1,
            "mode": protocol.MODE,
            "run_id": run,
            "status": "passed",
            "recovery_status": "passed",
            "fixture_identity": self.handoff["fixture_identity"],
            "handles": HANDLES,
            "handoff_path": protocol.HANDOFF_NAME,
            "recovery_physical_path": f"recovery/{recovery_ten.PHYSICAL_NAME}",
            "base_physical_paths": [
                f"{c.directory}/physical-evidence.json" for c in self.controls
            ],
            "primary_error": None,
            "cleanup_errors": [],
            "boundaries": [
                *self.handoff["boundaries"],
                {"boundary": "handoff-ready", "monotonic": 92.0},
                {**self.handoff["boundaries"][-1], "monotonic": 93.0},
                {
                    "boundary": "begin-authorized",
                    "monotonic": 101.0,
                    "native_signal": self.begin["native_signal"],
                },
            ],
        }
        write_document(self.root / protocol.PHYSICAL_NAME, self.physical)
        lines = [
            "rf-host:bootstrap:" + json.dumps(self.control_boot),
            "rf-recovery-ten:native:bootstrap:" + json.dumps(self.boot),
            f"rf-lab-full:runtime:session={run}:nonce={nonce}:phase=start",
            f"rf-lab-suite:runtime:session={run}:nonce={nonce}:phase=start",
        ]
        flows = [
            MARKERS,
            base.CONNECT_MARKERS,
            GATT_MARKERS,
            IO_MARKERS,
            base.RSSI_MARKERS,
            base.NOTIFICATION_MARKERS,
            base.INDICATION_MARKERS,
            base.PASSIVE_DISCONNECT_MARKERS,
            controlled_markers(CANCELLATION_MARKERS, self.controls[0]),
            [l.removeprefix(api.PREFIX) for l in self.watchdog.text.splitlines()],
        ]
        for i, (mode, markers) in enumerate(zip(protocol.MODES, flows, strict=True)):
            lines.append(f"rf-lab-suite:flow-started:index={i}:mode={mode}")
            if i < 9:
                lines.append(
                    f"rf:start:serviceUuid={self.plan.service_uuid}:expectedLocalName={self.h.fixture._profile.local_name}:initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004"
                )
            for marker in markers:
                marker = marker.replace(base.SERVICE, self.plan.service_uuid).replace(
                    "localName=missing",
                    "localName=" + self.h.fixture._profile.local_name,
                )
                for c in self.controls:
                    for p in c.phases:
                        if marker == c.marker("observed", p):
                            lines.append(
                                "rf-host:signal:"
                                + json.dumps(
                                    {**self.control_boot, **asdict(c), "phase": p}
                                )
                            )
                lines.append(marker)
            lines.append(f"rf-lab-suite:flow-passed:index={i}:mode={mode}")
        lines.extend(
            [
                f"rf-lab-suite:runtime:session={run}:nonce={nonce}:phase=close",
                protocol.BASE_PASSED,
                f"rf-lab-full:recovery-awaiting:runId={run}",
                "rf-lab-full:native:signal:" + json.dumps(self.begin["native_signal"]),
                f"rf-lab-full:recovery-ready:runId={run}:source=linking",
            ]
        )
        lines.extend(
            l.removeprefix(api.PREFIX)
            for l in synthetic_events(self.h)
            if not l.startswith(recovery_ten.PREFIX + "native:bootstrap:")
        )
        lines.extend(
            [
                f"rf-lab-full:runtime:session={run}:nonce={nonce}:phase=close",
                f"rf-lab-full:passed:flows=10:cycles=10:runId={run}",
            ]
        )
        header = (
            "09-27 12:34:56.789    42    43 I ReactNativeJS: "
            if platform == "android"
            else ""
        )
        self.text = "".join(header + api.PREFIX + l + "\n" for l in lines)
        self.capture = {
            "scenario": "full-suite",
            "status": "passed",
            "base_status": "passed",
            "capture_pid": 8001,
            "command": ["adb", "logcat", "--pid=42"]
            if platform == "android"
            else ["xcrun", "devicectl", "--console", "--terminate-existing"],
            "control_bootstrap": self.control_boot,
            "control_actions": [
                {
                    "control": asdict(c),
                    "phase": p,
                    "requested_at": 10.0 + i,
                    "native_ack_at": 10.1 + i,
                    "js_ack_at": 10.2 + i,
                }
                for i, (c, p) in enumerate(
                    (c, p) for c in self.controls for p in c.phases
                )
            ],
            "control_signals": [
                {**self.control_boot, **asdict(c), "phase": p}
                for c in self.controls
                for p in c.phases
            ],
            "control_js_acknowledged": [
                c.marker("observed", p) for c in self.controls for p in c.phases
            ],
            "begin": self.begin,
            "handoff": self.handoff,
            "host_cycle_actions": list(range(10)),
            "runtime_nonce": nonce,
        }
        self.owner = {
            "scenario": "full-suite",
            "status": "passed",
            "pid": 42,
            "executable": self.boot["executable"],
            "bootstrap": self.boot,
            "platform": platform,
            "device": "physical-device",
            "device_identifier": "official-device-identifier",
            "native_acknowledged": list(range(10)),
            "js_acknowledged": list(range(10)),
            "install_json": str(directory / "device-install.json"),
        }
        if platform == "ios":
            write_document(
                Path(self.owner["install_json"]),
                document(
                    "devicectl.device.install.app",
                    {
                        "installedApplications": [
                            {
                                "bundleID": self.boot["bundle_id"],
                                "installationURL": IOS_URL,
                            }
                        ]
                    },
                ),
            )
            for boundary in ("bootstrap", "begin-before"):
                write_document(
                    self.log.with_suffix(f".{boundary}-processes.json"),
                    document(
                        "devicectl.device.info.processes",
                        {
                            "runningProcesses": [
                                {
                                    "processIdentifier": 42,
                                    "executable": self.boot["executable"],
                                }
                            ]
                        },
                    ),
                )
        for value in (self.capture, self.owner):
            value.update(run_id=run, platform=platform, device="physical-device")
        self.publish()

    def publish(self) -> None:
        self.log.write_text(self.text, encoding="utf-8")
        self.log.with_suffix(".raw.log").write_text(
            "unrelated raw line\n" + self.text, encoding="utf-8"
        )
        (self.log.parent / "recovery-ten-runtime.log").write_text(
            api.recovery_projection(self.text), encoding="utf-8"
        )
        (self.root / "runtime.log").write_text(
            "".join(l + "\n" for l in api.normalized(self.text)), encoding="utf-8"
        )
        (self.root / "recovery" / "runtime.log").write_text(
            "".join(
                l + "\n" for l in api.normalized(api.recovery_projection(self.text))
            ),
            encoding="utf-8",
        )
        write_document(self.log.with_suffix(".capture.json"), self.capture)
        write_document(self.log.with_suffix(".identity.json"), self.owner)

    def verify(self) -> dict:
        return api.verify(
            self.text,
            log=self.log,
            platform=self.platform,
            run_id=self.plan.session_id,
            service_uuid=self.plan.service_uuid,
            fixture_request=self.plan.request_path,
            fixture_identity=self.identity,
            host_cycles=tuple(range(10)),
        )
