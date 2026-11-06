"""Real CCC provider/relay with synthetic SDK children; never installation or RF.

Reuse the response-write SDK pump. Only the independent ledger is an explicit
boundary here; provider runtime facts, controls and physical evidence are real
production writers over the owner-local typed BTP emulator.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import create_autospec, patch

from tests.peripherals.nrf52840 import ccc_enable_reject as protocol
from tests.peripherals.nrf52840.support.ccc_enable_reject_emulator import (
    Harness as Peripheral,
)
from tools import react_native
from tools.react_native import reject_capture, write_reject_ledger
from tools.tests.react_native.support.write_reject_capture import Replay as WriteReplay

PHASES = protocol.PHASES


class Phone(Peripheral):
    def ready(self, request):
        if request == "silence-done":
            time.sleep(1.02)
        self.requests.append(request)
        self.lines.append(self.control.marker("ready", request))


def capture_boundary(ledger):
    """Load the actual adapter with a private ledger boundary, not sys.modules."""
    path = Path(reject_capture.__file__).with_name("ccc_enable_reject_capture.py")
    spec = importlib.util.spec_from_file_location("synthetic_ccc_capture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.object(react_native, "ccc_enable_reject_ledger", ledger, create=True):
        spec.loader.exec_module(module)
    return module


class Replay(WriteReplay):
    protocol = protocol
    scenario = "ccc-enable-reject"

    def __init__(self, root, platform="android", fault=""):
        self.ledger = ModuleType("synthetic_ccc_enable_reject_ledger")
        self.ledger.verify = create_autospec(
            write_reject_ledger.verify, return_value={"synthetic_ledger_boundary": True}
        )
        self.api = capture_boundary(self.ledger)
        self.capture_type = self.api.CccEnableRejectCapture
        super().__init__(root, platform, fault)

    def _new_phone(self, root, platform):
        return Phone(root, platform, inline=False)

    def _new_owner(self):
        return protocol.CccEnableReject(
            self.phone.fixture, self.root, self.phone.handles
        )

    def signal(self, phase):
        if self.fault != "wrong-flow":
            super().signal(phase)
            return
        original = self.identity
        self.identity = {**original, "flow": 10, "mode": "rf-write-reject"}
        try:
            super().signal(phase)
        finally:
            self.identity = original
