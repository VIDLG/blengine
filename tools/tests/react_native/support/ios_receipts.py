"""Synthetic iOS boot, process and install receipts for host relay tests."""

from __future__ import annotations

import json
from pathlib import Path

from tests.peripherals.nrf52840.cancellation_control import Control
from tools.react_native.host_contract import APP_NAME, BUNDLE_ID, SCHEME

CONTROL = Control(
    "12345678-1234-4234-8234-123456789abc",
    8,
    "rf-cancellation",
    "11111111-1111-4111-8111-111111111111",
)

DEVICE_IDENTIFIER = "130256F7-7150-5A81-A906-6806AAFBFADA"

EXECUTABLE = (
    f"file:///private/var/containers/Bundle/Application/install/{APP_NAME}/{SCHEME}"
)


def ios_bootstrap(control: Control = CONTROL) -> dict:
    """Scoped native boot record, independent of console-exit launch JSON."""
    return {
        "session": control.session,
        "flow": control.flow,
        "mode": control.mode,
        "token": control.token,
        "bundle_id": BUNDLE_ID,
        "pid": 42,
        "boot_id": "22222222-2222-4222-8222-222222222222",
        "executable": EXECUTABLE,
    }


def write_ios_install(path: Path) -> None:
    """Official-format install receipt for the exact executable on the selected device."""
    path.write_text(
        json.dumps(
            {
                "info": {
                    "outcome": "success",
                    "commandType": "devicectl.device.install.app",
                    "arguments": [
                        "devicectl",
                        "device",
                        "install",
                        "app",
                        "--device",
                        "device",
                    ],
                },
                "result": {
                    "deviceIdentifier": DEVICE_IDENTIFIER,
                    "installedApplications": [
                        {
                            "bundleID": BUNDLE_ID,
                            "installationURL": EXECUTABLE.removesuffix(SCHEME),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )


def ios_process_document() -> dict:
    """Official process-list schema; neither a launch receipt nor name-based selection."""
    return {
        "info": {
            "outcome": "success",
            "commandType": "devicectl.device.info.processes",
            "arguments": [
                "devicectl",
                "device",
                "info",
                "processes",
                "--device",
                "device",
            ],
        },
        "result": {
            "deviceIdentifier": DEVICE_IDENTIFIER,
            "runningProcesses": [{"processIdentifier": 42, "executable": EXECUTABLE}],
        },
    }


def host_marker(kind: str, record: dict) -> str:
    return f"BLEHUB_NITRO_RUNTIME rf-host:{kind}:{json.dumps(record)}"
