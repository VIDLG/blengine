"""Private CCC runner harness with real control preparation, no installation/RF."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

from tests.peripherals import nrf52840
from tests.peripherals.nrf52840 import ccc_disable_reject as protocol
from tools import react_native
from tools.react_native import write_reject_capture, write_reject_ledger
from tools.tests.react_native.support.write_reject_parent import Harness as WriteHarness


class Harness(WriteHarness):
    scenario = "ccc-disable-reject"

    def __init__(self, root, platform):
        super().__init__(root, platform)
        self.protocol = SimpleNamespace(
            MODE=protocol.MODE,
            CONTROL_NAME=protocol.CONTROL_NAME,
            PHYSICAL_NAME=protocol.PHYSICAL_NAME,
            prepare_control=Mock(side_effect=protocol.prepare_control),
        )
        self.capture = create_autospec(
            write_reject_capture.capture, side_effect=self._write_capture
        )
        self.checker = create_autospec(
            write_reject_ledger.verify, side_effect=self._write_checker
        )

    def run(self, *, timeout=180):
        with (
            patch.object(nrf52840, "ccc_disable_reject", self.protocol),
            patch.object(
                react_native,
                "ccc_disable_reject_capture",
                SimpleNamespace(capture=self.capture),
                create=True,
            ),
            patch.object(
                react_native,
                "ccc_disable_reject_ledger",
                SimpleNamespace(verify=self.checker),
                create=True,
            ),
        ):
            return super().run(timeout=timeout)
