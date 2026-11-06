"""Fixture SDK and UI samples for prepared formal scan integration tests."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import direct_fixture, fixture_session
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.fixture_attestation import parse_attestation
from tests.peripherals.nrf52840.fixture_attestation_wire import encode_attestation
from tests.peripherals.nrf52840.gap_commands import CONTROLLER_INFO
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.resource_lease import DeviceLease
from tests.peripherals.nrf52840.support.direct_fixture_resources import resources
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.tests.react_native.support.bleng_ui import ScanDriver, advertisement


@contextmanager
def prepared_fixture(root: Path, run_id: str):
    root.mkdir()
    contract, profile = write_v2_inputs(root, "b")
    with patch.object(
        fixture_session, "identity_for_port", return_value="jlink:123456"
    ):
        plan = fixture_session.prepare_session(
            contract,
            profile,
            root / "session",
            port="test-port",
            service_uuid=load_profile(profile).service_uuid,
            session_id=run_id,
        )
    events: list[str] = []
    with (
        resources(events) as wire,
        patch.object(direct_fixture, "load_profile", wraps=load_profile),
        patch.object(direct_fixture, "NrfDoctor") as doctor,
    ):
        doctor.return_value.run.return_value = SimpleNamespace(
            role_handles={"updates": 37}
        )
        wire.session.pop_event.return_value = None
        lease = DeviceLease("jlink", "123456", purpose="synthetic prepared scan")
        lease._pid, lease._held = os.getpid(), True
        lease._process = Mock()
        lease._process.poll.return_value = None
        wire.owner.require.side_effect = lease.require
        installed = json.loads(contract.read_bytes())
        installed.pop("fixture_role")
        attestation = encode_attestation(parse_attestation(installed))
        limits = wire.session.request.return_value
        wire.session.request.side_effect = lambda service, opcode, **kwargs: (
            BtpFrame(255, 1, 255, attestation)
            if (service, opcode) == (255, 1)
            else limits
        )
        wire.gap.read_controller_info.return_value = CONTROLLER_INFO.pack(
            bytes(6),
            1 << 10,
            0,
            bytes(3),
            bytes(249),
            bytes(11),
        )
        fixture = direct_fixture.DirectFixture(
            plan.port, plan.profile_path, fixture_session=plan
        )
        try:
            fixture.start()
            yield fixture, plan, wire, events
        finally:
            # Assertion failures must not fall back to legacy disconnect/reset.
            fixture._resources.close()


def scan_driver(platform: str, device: str, app_id: str, plan):
    driver = ScanDriver(
        platform,
        [advertisement(plan.contract.local_name, (plan.contract.service_uuid,))],
    )
    driver.app_id = driver.foreground = app_id
    driver.capabilities = {
        "platformName": "iOS" if platform == "ios" else "Android",
        "appium:udid": device,
        "appium:bundleId" if platform == "ios" else "appium:appPackage": app_id,
        "appium:autoLaunch": False,
        "appium:forceAppLaunch": False,
        "appium:shouldTerminateApp": False,
        "appium:noReset": True,
    }
    driver.command_executor = SimpleNamespace(
        client_config=SimpleNamespace(timeout=5.0)
    )
    return driver
