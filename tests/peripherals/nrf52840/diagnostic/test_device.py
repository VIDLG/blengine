"""Hardware-free tests for the maintained pending-read device boundaries."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.diagnostic import device
from tests.peripherals.nrf52840.diagnostic.profile import load_diagnostic_profile
from tools.react_native import pending_read_ledger as ledger

REAL_LOG = (
    Path(__file__).resolve().parents[4]
    / ".work/nrf-att-hold-dk/rn-pending-read-reload-d8792205409b406cb2bca82fa94bc917.attempt.log"
)
RUN_ID = "d8792205-409b-406c-b2bc-a82fa94bc917"
PROBE_ID = "34b60b5a-e045-490b-b309-74a75e7e0a79"


class MaintainedDeviceTests(unittest.TestCase):
    def test_diagnostic_profile_is_fd10_and_matches_pending_read_host(self) -> None:
        profile = load_diagnostic_profile(device.PROFILE)
        self.assertEqual(profile.service_uuid, device.SERVICE_UUID)
        self.assertEqual(profile.local_name, "BleHubAttHoldDK")
        device.validate_host_contract()
        with (
            patch.object(device, "HOST_EXPECTED_LOCAL_NAME", "wrong-host-name"),
            self.assertRaisesRegex(
                device.DiagnosticError, "pending-read reload profile mismatch"
            ),
        ):
            device.validate_host_contract()

    def test_run_refuses_local_image_mismatch_before_lock_or_hardware(self) -> None:
        with (
            patch.object(
                device,
                "validate_local_image",
                side_effect=device.DiagnosticError("no local image"),
            ),
            patch.object(device, "claim_lock") as claim,
            patch.object(device, "detect_btp_port") as detect,
            self.assertRaisesRegex(device.DiagnosticError, "no local image"),
        ):
            device.run(
                "00008110-0016655002D9401E",
                "123456789",
                "0" * 64,
            )
        claim.assert_not_called()
        detect.assert_not_called()

    def test_probe_serial_validation_is_explicit(self) -> None:
        for value in ("", "auto", "1234x", "1.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                device.validate_probe_serial(value)
        device.validate_probe_serial("123456789")

    def test_profile_resets_owned_dynamic_server_before_provision(self) -> None:
        gatt = Mock()
        gatt.get_attributes.return_value = (Mock(handle=41, permissions=0x43),)
        with patch.object(
            device, "provision", return_value={"read-write": 41}
        ) as provision:
            device._prepare_profile(gatt)
        gatt.reset_server.assert_called_once_with()
        provision.assert_called_once()
        self.assertEqual(gatt.mock_calls[0], unittest.mock.call.reset_server())

    def test_profile_refuses_failed_reset_without_provision(self) -> None:
        gatt = Mock()
        gatt.reset_server.side_effect = device.BtpCommandError("connected")
        with (
            patch.object(device, "provision") as provision,
            self.assertRaisesRegex(device.BtpCommandError, "connected"),
        ):
            device._prepare_profile(gatt)
        provision.assert_not_called()

    def test_btp_configuration_requires_private_read_hold_commands(self) -> None:
        class FakeCore:
            def __init__(self, _session: object) -> None:
                pass

            def read_supported_commands(self, _service: int) -> int:
                return 0

            def register_service(self, _service: int) -> None:
                raise AssertionError("unexpected registration")

        with (
            patch.object(device, "CoreCommands", FakeCore),
            self.assertRaisesRegex(device.DiagnosticError, "missing one or more"),
        ):
            device.configure_btp(object())  # type: ignore[arg-type]

    def test_btp_configuration_requires_dynamic_server_reset(self) -> None:
        class FakeCore:
            def __init__(self, _session: object) -> None:
                pass

            def read_supported_commands(self, service: int) -> int:
                if service == device.GATT:
                    return device.READ_HOLD_COMMAND_MASK & ~(1 << 0x08)
                return 0

        with (
            patch.object(device, "CoreCommands", FakeCore),
            self.assertRaisesRegex(device.DiagnosticError, "missing one or more"),
        ):
            device.configure_btp(object())  # type: ignore[arg-type]

    def test_btp_configuration_accepts_private_read_hold_commands(self) -> None:
        class FakeCore:
            def __init__(self, _session: object) -> None:
                pass

            def read_supported_commands(self, service: int) -> int:
                return device.READ_HOLD_COMMAND_MASK if service == device.GATT else 0

            def register_service(self, _service: int) -> None:
                raise AssertionError("unexpected registration")

        with patch.object(device, "CoreCommands", FakeCore):
            device.configure_btp(object())  # type: ignore[arg-type]

    def test_detect_btp_port_probes_all_vcoms_before_rejecting_two_responders(
        self,
    ) -> None:
        probe = device.ProbeIdentity(1, "usb-location", ("/dev/A", "/dev/B"))
        opened: list[str] = []
        required = 1 | (1 << device.GAP) | (1 << device.GATT)

        @contextmanager
        def fake_open(port: str):
            opened.append(port)
            yield port

        class FakeCore:
            def __init__(self, session: str) -> None:
                self.session = session

            def read_supported_services(self) -> int:
                return required

        with (
            patch.object(device, "verify_probe", return_value=probe),
            patch.object(device, "open_btp_port", side_effect=fake_open),
            patch.object(
                device, "SerialByteTransport", side_effect=lambda value: value
            ),
            patch.object(device, "FramedTransport", side_effect=lambda value: value),
            patch.object(device, "BtpSession", side_effect=lambda value: value),
            patch.object(device, "CoreCommands", FakeCore),
            self.assertRaisesRegex(device.DiagnosticError, "exactly one BTP VCOM"),
        ):
            device.detect_btp_port("123456789", requested_port="/dev/A")
        self.assertEqual(opened, ["/dev/A", "/dev/B"])

    def test_detect_btp_port_requires_requested_port_to_be_unique_responder(
        self,
    ) -> None:
        probe = device.ProbeIdentity(1, "usb-location", ("/dev/A", "/dev/B"))
        required = 1 | (1 << device.GAP) | (1 << device.GATT)

        @contextmanager
        def fake_open(port: str):
            yield port

        class FakeCore:
            def __init__(self, session: str) -> None:
                self.session = session

            def read_supported_services(self) -> int:
                return required if self.session == "/dev/A" else 0

        with (
            patch.object(device, "verify_probe", return_value=probe),
            patch.object(device, "open_btp_port", side_effect=fake_open),
            patch.object(
                device, "SerialByteTransport", side_effect=lambda value: value
            ),
            patch.object(device, "FramedTransport", side_effect=lambda value: value),
            patch.object(device, "BtpSession", side_effect=lambda value: value),
            patch.object(device, "CoreCommands", FakeCore),
            self.assertRaisesRegex(
                device.DiagnosticError, "not the unique BTP responder"
            ),
        ):
            device.detect_btp_port("123456789", requested_port="/dev/B")

    def test_notification_barriers_use_real_log_prefixes_for_both_stages(self) -> None:
        text = REAL_LOG.read_text(encoding="utf-8") if REAL_LOG.is_file() else ""
        if not text:
            self.skipTest("saved FD10 pending-read log is absent")
        process = Mock()
        process.poll.return_value = None
        for stage, ready, received in (
            ("reload", "external-ready", "notification-received"),
            ("arm", "arm-ready", "arm-notification-received"),
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                log = Path(temporary) / f"{stage}.attempt.log"
                marker = f"{device.ledger.PREFIX}{device.ledger.MODE}:{ready}"
                lines = text.splitlines()
                ready_index = next(
                    index for index, line in enumerate(lines) if line == marker
                )
                log.write_text(
                    "\n".join(lines[: ready_index + 1]) + "\n", encoding="utf-8"
                )

                received_marker = (
                    f"{device.ledger.PREFIX}{device.ledger.MODE}:{received}"
                )

                def post(
                    _command: list[str],
                    *,
                    log_path: Path = log,
                    marker: str = received_marker,
                    **_kwargs: object,
                ) -> None:
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(marker + "\n")

                with patch.object(device.subprocess, "run", side_effect=post) as run:
                    device.post_notification(
                        "xcrun",
                        "00008110-0016655002D9401E",
                        "dev.blehub.rn.reload." + RUN_ID,
                        log,
                        process,
                        stage,
                        device.time.monotonic() + 1,
                        RUN_ID,
                    )
                run.assert_called_once()
                self.assertIn(
                    f"{device.ledger.PREFIX}{device.ledger.MODE}:{received}\n",
                    log.read_text(encoding="utf-8"),
                )

    def test_lock_never_takes_over_or_releases_changed_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rn-ios-scan.lock"
            path.mkdir()
            (path / ".owner").write_text("another-owner", encoding="ascii")
            with self.assertRaisesRegex(device.DiagnosticError, "already present"):
                device.claim_lock(path)
            self.assertEqual(
                (path / ".owner").read_text(encoding="ascii"), "another-owner"
            )

    def test_owned_runtime_termination_remains_bounded_after_sigkill(self) -> None:
        process = Mock(pid=321)
        process.poll.return_value = None
        process.wait.side_effect = [
            device.subprocess.TimeoutExpired("runtime", 10),
            device.subprocess.TimeoutExpired("runtime", 5),
        ]
        with (
            patch.object(device.os, "killpg") as killpg,
            self.assertRaises(device.subprocess.TimeoutExpired),
        ):
            device._stop_owned_process(process)
        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(321, device.signal.SIGTERM),
                unittest.mock.call(321, device.signal.SIGKILL),
            ],
        )
        self.assertEqual(
            process.wait.call_args_list,
            [unittest.mock.call(timeout=10), unittest.mock.call(timeout=5)],
        )

    def test_owned_lock_release_requires_unchanged_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rn-ios-scan.lock"
            claim = device.claim_lock(path)
            (path / ".owner").write_text("replaced", encoding="ascii")
            with self.assertRaisesRegex(device.DiagnosticError, "ownership changed"):
                claim.release()
            self.assertTrue(path.is_dir())
            self.assertEqual((path / ".owner").read_text(encoding="ascii"), "replaced")

    def test_saved_fd10_log_regression_uses_maintained_ledger(self) -> None:
        if not REAL_LOG.is_file():
            self.skipTest("saved FD10 pending-read log is absent")
        text = REAL_LOG.read_text(encoding="utf-8")
        self.assertEqual(
            ledger.verify_ledger(
                text,
                run_id=RUN_ID,
                probe_id=PROBE_ID,
                post_steps=("reload", "arm"),
                old_characteristic="13",
            ),
            ("13", "13"),
        )


if __name__ == "__main__":
    unittest.main()
