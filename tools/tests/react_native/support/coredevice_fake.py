"""A fake iPhone for the lanes that ask CoreDevice about Apps and processes.

This is test support, never evidence: it answers `devicectl` questions from scripted lists and
writes the same official receipt shape the real tool does, so a lane parses it with its own
validator (`ios_sdk.official_result`) rather than with a test-only shortcut. Synthetic contract
tests and device-observed regressions both use it, which is why it is here and not inside
either test module.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_app_cleanup as cleanup

BUNDLE = "FWVYQ287W5.bleng-rn-host"
DEVICE = "00008110-000C61160C39401E"
DIRECTORY = "file:///private/var/containers/Bundle/Application/1111-2222/candidate.app"
# Each CoreDevice question and the result key its answer arrives under.
QUESTIONS = {
    "devicectl.device.info.apps": "apps",
    "devicectl.device.info.processes": "runningProcesses",
}


def app(
    bundle: str = BUNDLE,
    url: str = "file:///private/var/containers/Bundle/Application/1111-2222/candidate.app/",
) -> dict[str, str]:
    """One row of an installed-Apps inventory."""
    return {"bundleIdentifier": bundle, "url": url}


def running(pid: int = 7) -> dict[str, object]:
    """One process of this installation, as the process inventory reports it."""
    return {"executable": DIRECTORY + "/BleHubBlengHost", "processIdentifier": pid}


class Lease:
    """A stand-in for the radio lease: it records nothing and owns nothing."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def acquire(self) -> None:
        pass

    def require_owner(self) -> None:
        pass

    def finish_observation(self, primary, *, finalize_owned) -> list[str]:
        return []


class Device:
    """Answers CoreDevice queries from scripted lists, recording each official receipt."""

    def __init__(
        self, apps: list[list[dict]], processes: list[list[dict]] | None = None
    ) -> None:
        self.answers = {"apps": list(apps), "runningProcesses": list(processes or [])}
        self.kinds: list[str] = []

    def run_command(self, argv: list[str], *, cwd, log: Path, timeout: float) -> None:
        kind = ".".join(argv[1:5])
        self.kinds.append(kind)
        result: dict = {"deviceIdentifier": "130256F7-7150-5A81-A906-6806AAFBFADA"}
        if kind in QUESTIONS:
            key = QUESTIONS[kind]
            if not self.answers[key]:
                raise AssertionError(f"unexpected {kind} query")
            result[key] = self.answers[key].pop(0)
        document = {
            "info": {"outcome": "success", "commandType": kind, "arguments": argv[1:]},
            "result": result,
        }
        Path(argv[argv.index("--json-output") + 1]).write_text(json.dumps(document))


def release(device: Device, output: Path, **changes) -> dict:
    """Run the real `release_app` against the fake; return the receipt it published.

    A refusal is not raised to the caller: the receipt under the output root says what
    happened either way, and that receipt is what the tests read.
    """
    arguments = {
        "device": DEVICE,
        "output": output,
        "deadline": time.monotonic() + 60,
        "bundle_id": BUNDLE,
        "run_command": device.run_command,
    }
    arguments.update(changes)
    with (
        patch.object(cleanup, "IosRadioLease", Lease),
        patch.object(cleanup.time, "sleep", lambda seconds: None),
    ):
        try:
            cleanup.release_app(**arguments)
        except cleanup.FormalAppCleanupError:
            pass
    return json.loads((output / "cleanup.json").read_text())


__all__ = [
    "BUNDLE",
    "DEVICE",
    "DIRECTORY",
    "QUESTIONS",
    "Device",
    "Lease",
    "app",
    "release",
    "running",
]
