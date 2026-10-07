from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tests.peripherals.nrf52840 import direct_fixture, provider


class ProviderRegressionTests(unittest.TestCase):
    def test_start_uses_canonical_profile_after_source_move(self) -> None:
        # Exercise command construction without launching nrftest or opening a port.
        profile = json.loads(provider.PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(profile["profile_id"], "blehub-nrf52840-indication-v2")
        self.assertTrue((provider.PROJECT_ROOT / "Cargo.toml").is_file())
        self.assertEqual(
            provider.WORK_ROOT.relative_to(provider.PROJECT_ROOT).parts,
            (".work", "peripheral", "nrf52840"),
        )

        def launch(*args: object, **kwargs: object) -> SimpleNamespace:
            log = kwargs["stdout"]
            log.write(provider.READY_MARKER + "test\n")
            log.flush()
            return SimpleNamespace(pid=43210)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "WORK_ROOT", Path(temporary)),
            patch.object(provider, "PID_FILE", Path(temporary) / "fixture.pid"),
            patch.object(provider, "LOG_FILE", Path(temporary) / "fixture.log"),
            patch.object(provider, "_nrftest_root", return_value=Path(temporary)),
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(provider, "_alive", return_value=True),
            patch.object(provider.os, "getpgid", return_value=43210),
            patch.object(provider.shutil, "which", return_value="pixi"),
            patch.object(provider.subprocess, "Popen", side_effect=launch) as popen,
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.start(ready_timeout=1, rf_timeout=10)
            command = popen.call_args.args[0]
            self.assertEqual(
                command[command.index("--profile") + 1], str(provider.PROFILE)
            )
            self.assertIn("nrf52840_fixture=ready", output.getvalue())
            self.assertEqual(
                (Path(temporary) / "fixture.pid").read_text(encoding="utf-8"),
                "43210 43210\n",
            )

    def test_direct_start_rolls_back_the_exact_spawned_identity_on_readiness_failure(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "WORK_ROOT", Path(temporary)),
            patch.object(provider, "PID_FILE", Path(temporary) / "fixture.pid"),
            patch.object(provider, "LOG_FILE", Path(temporary) / "fixture.log"),
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(
                provider.subprocess,
                "Popen",
                return_value=SimpleNamespace(pid=43210),
            ),
            patch.object(provider.os, "getpgid", return_value=43210),
            patch.object(
                provider,
                "_wait_ready",
                side_effect=provider.Nrf52840Error("readiness failed"),
            ),
            patch.object(provider, "stop") as stop,
            self.assertRaisesRegex(provider.Nrf52840Error, "readiness failed"),
        ):
            provider.direct_start(str(Path(temporary) / "coordination"), "recovery-ios")

        stop.assert_called_once_with(expected_identity=(43210, 43210))

    def test_direct_start_does_not_stop_an_existing_provider_on_readiness_failure(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "WORK_ROOT", Path(temporary)),
            patch.object(provider, "PID_FILE", Path(temporary) / "fixture.pid"),
            patch.object(provider, "LOG_FILE", Path(temporary) / "fixture.log"),
            patch.object(
                provider,
                "_process_identity",
                return_value=(43210, 43210),
            ),
            patch.object(
                provider,
                "_fixture_state",
                return_value=provider._PROCESS_RUNNING,
            ),
            patch.object(
                provider,
                "_wait_ready",
                side_effect=provider.Nrf52840Error("readiness failed"),
            ),
            patch.object(provider, "stop") as stop,
            self.assertRaisesRegex(provider.Nrf52840Error, "readiness failed"),
        ):
            provider.direct_start()

        stop.assert_not_called()

    def test_stop_owned_rejects_a_changed_identity_before_signaling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("999 999\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(provider.os, "killpg") as killpg,
                self.assertRaisesRegex(provider.Nrf52840Error, "identity changed"),
            ):
                provider.stop(expected_identity=(43210, 43210))
            killpg.assert_not_called()

    def test_stop_retains_marker_for_a_reused_pid_or_mismatched_pgid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("43210 43210\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(
                    provider,
                    "_fixture_state",
                    return_value=provider._PROCESS_MISMATCH,
                ),
                patch.object(provider.os, "killpg") as killpg,
                self.assertRaisesRegex(provider.Nrf52840Error, "mismatch"),
            ):
                provider.stop()
            self.assertTrue(pid_file.exists())
            killpg.assert_not_called()

    def test_wait_for_exit_rejects_mismatch_without_waiting(self) -> None:
        with (
            patch.object(
                provider,
                "_fixture_state",
                return_value=provider._PROCESS_MISMATCH,
            ),
            patch.object(provider.time, "monotonic", return_value=0),
            patch.object(provider.time, "sleep") as sleep,
        ):
            self.assertFalse(provider._wait_for_exit(43210, 43210, 15))
        sleep.assert_not_called()

    def test_stop_waits_through_transient_unknown_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("43210 43210\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(
                    provider,
                    "_fixture_state",
                    side_effect=[
                        provider._PROCESS_RUNNING,
                        provider._PROCESS_UNKNOWN,
                        provider._PROCESS_EXITED,
                    ],
                ),
                patch.object(provider.time, "monotonic", side_effect=[0, 1]),
                patch.object(provider.time, "sleep") as sleep,
                patch.object(provider.os, "killpg") as killpg,
            ):
                provider.stop()
            self.assertFalse(pid_file.exists())
            sleep.assert_called_once_with(0.2)
            killpg.assert_called_once_with(43210, provider.signal.SIGTERM)

    def test_stop_retains_marker_when_unknown_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("43210 43210\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(
                    provider,
                    "_fixture_state",
                    side_effect=[
                        provider._PROCESS_RUNNING,
                        provider._PROCESS_UNKNOWN,
                        provider._PROCESS_UNKNOWN,
                    ],
                ),
                patch.object(provider.time, "monotonic", side_effect=[0, 15]),
                patch.object(provider.time, "sleep") as sleep,
                patch.object(provider.os, "killpg") as killpg,
                self.assertRaisesRegex(
                    provider.Nrf52840Error, "state=unknown; retaining"
                ),
            ):
                provider.stop()
            self.assertTrue(pid_file.exists())
            sleep.assert_not_called()
            killpg.assert_called_once_with(43210, provider.signal.SIGTERM)

    def test_stop_cleans_marker_when_kill_races_with_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("43210 43210\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(
                    provider,
                    "_fixture_state",
                    side_effect=[
                        provider._PROCESS_RUNNING,
                        provider._PROCESS_RUNNING,
                        provider._PROCESS_EXITED,
                    ],
                ),
                patch.object(
                    provider,
                    "_wait_for_exit",
                    side_effect=[False, True],
                ),
                patch.object(
                    provider.os,
                    "killpg",
                    side_effect=[None, PermissionError("group already exited")],
                ) as killpg,
            ):
                provider.stop()
            self.assertFalse(pid_file.exists())
            self.assertEqual(
                killpg.call_args_list,
                [
                    call(43210, provider.signal.SIGTERM),
                    call(43210, provider.signal.SIGKILL),
                ],
            )

    def test_stop_retains_marker_when_signal_permission_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("43210 43210\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(
                    provider,
                    "_fixture_state",
                    side_effect=[
                        provider._PROCESS_RUNNING,
                        provider._PROCESS_RUNNING,
                    ],
                ),
                patch.object(
                    provider.os,
                    "killpg",
                    side_effect=PermissionError("not owner"),
                ) as killpg,
                self.assertRaisesRegex(provider.Nrf52840Error, "cannot signal"),
            ):
                provider.stop()
            self.assertTrue(pid_file.exists())
            killpg.assert_called_once_with(43210, provider.signal.SIGTERM)

    def test_stop_retains_marker_when_owned_process_group_stays_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "fixture.pid"
            pid_file.write_text("43210 43210\n", encoding="utf-8")
            with (
                patch.object(provider, "PID_FILE", pid_file),
                patch.object(
                    provider,
                    "_fixture_state",
                    return_value=provider._PROCESS_RUNNING,
                ),
                patch.object(provider, "_wait_for_exit", return_value=False),
                patch.object(provider.os, "killpg") as killpg,
                self.assertRaisesRegex(provider.Nrf52840Error, "did not exit"),
            ):
                provider.stop()
            self.assertTrue(pid_file.exists())
            self.assertEqual(killpg.call_count, 2)

    def test_doctor_is_explicit_and_closes_serial_transport(self) -> None:
        serial = SimpleNamespace(close=Mock())
        result = SimpleNamespace(
            profile_id="blehub-nrf52840-basic-gatt-v1",
            supported_services=6,
            role_handles={"read-write": 37},
        )
        with (
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(provider, "open_serial", return_value=serial),
            patch.object(provider, "run_profile_doctor", return_value=result) as doctor,
            patch.object(provider, "BtpSession") as session,
            patch.object(provider, "FramedTransport") as framed,
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.doctor()
        doctor.assert_called_once()
        serial.close.assert_called_once_with()
        framed.assert_called_once_with(serial)
        session.assert_called_once_with(framed.return_value)
        self.assertIn("nrf52840_doctor=passed", output.getvalue())

    def test_exited_process_cannot_reuse_old_ready_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "fixture.log"
            log.write_text(provider.READY_MARKER + "old-run\n", encoding="utf-8")
            with (
                patch.object(provider, "LOG_FILE", log),
                patch.object(provider, "_alive", return_value=False),
                self.assertRaisesRegex(
                    provider.Nrf52840Error, "exited before readiness"
                ),
            ):
                provider._wait_ready(43210, 1)

    def test_running_process_without_log_reports_not_ready(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "LOG_FILE", Path(temporary) / "missing.log"),
            patch.object(
                provider,
                "_process_identity",
                return_value=(43210, 43210),
            ),
            patch.object(provider, "_alive", return_value=True),
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.status()
            self.assertIn("nrf52840_fixture=running", output.getvalue())
            self.assertIn("nrf52840_fixture_ready=false", output.getvalue())

    def test_parse_args_accepts_host_close_coordination_mode(self) -> None:
        for command in ("direct-start", "direct-serve"):
            with (
                self.subTest(command=command),
                patch.object(
                    provider.sys,
                    "argv",
                    [
                        "nrf52840-provider",
                        command,
                        "--coordination-mode",
                        "host-close",
                    ],
                ),
            ):
                args = provider.parse_args()
            self.assertEqual(args.coordination_mode, "host-close")

    def test_parse_args_accepts_recovery_ios_coordination_mode(self) -> None:
        for command in ("direct-start", "direct-serve"):
            with (
                self.subTest(command=command),
                patch.object(
                    provider.sys,
                    "argv",
                    [
                        "nrf52840-provider",
                        command,
                        "--coordination-mode",
                        "recovery-ios",
                    ],
                ),
            ):
                args = provider.parse_args()
            self.assertEqual(args.coordination_mode, "recovery-ios")

    def test_parse_args_accepts_recovery_ten_coordination_mode(self) -> None:
        for command in ("direct-start", "direct-serve"):
            with (
                self.subTest(command=command),
                patch.object(
                    provider.sys,
                    "argv",
                    [
                        "nrf52840-provider",
                        command,
                        "--coordination-mode",
                        "recovery-ten",
                    ],
                ),
            ):
                args = provider.parse_args()
            self.assertEqual(args.coordination_mode, "recovery-ten")

    def test_direct_serve_dispatches_recovery_ten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            coordination_path = Path(temporary) / "coordination"
            fixture = Mock()
            fixture._profile = SimpleNamespace(profile_id="test-profile")
            fixture._stop_requested = True
            fixture.start.return_value = {"updates": 37}
            with (
                patch.object(direct_fixture, "DirectFixture", return_value=fixture),
                patch.object(direct_fixture.signal, "signal"),
            ):
                direct_fixture.serve(
                    "test-port",
                    Path(temporary) / "profile.json",
                    coordination_path,
                    "recovery-ten",
                )

        fixture.run_recovery_coordination.assert_called_once_with(
            coordination_path, {"updates": 37}, cycles=10
        )
        fixture.stop.assert_called_once_with()

    def test_recovery_coordination_runs_all_requested_cycles(self) -> None:
        cycles = 10
        with tempfile.TemporaryDirectory() as temporary:
            coordination_path = Path(temporary) / "coordination"
            for cycle in range(cycles):
                directory = coordination_path / f"cycle-{cycle}"
                directory.mkdir(parents=True)
                for marker in (
                    "subscription-enabled",
                    "connected",
                    "passive-disconnected",
                    "cycle-passed",
                ):
                    (directory / marker).write_text("ready\n", encoding="ascii")

            fixture = object.__new__(direct_fixture.DirectFixture)
            fixture._stop_requested = False
            fixture._profile = direct_fixture.load_profile(provider.PROFILE)
            pending_events: list[SimpleNamespace] = []

            session = Mock()

            def pop_event() -> SimpleNamespace | None:
                return pending_events.pop(0) if pending_events else None

            session.pop_event.side_effect = pop_event
            session.poll_event.return_value = None

            gatt = Mock()

            def get_attributes() -> None:
                pending_events.append(
                    SimpleNamespace(
                        service=1,
                        opcode=0x82,
                        payload=b"\x00\x01\x02\x03\x04\x05\x06",
                    )
                )

            gatt.get_attributes.side_effect = get_attributes
            gap = Mock()

            def disconnect(*_: object) -> None:
                pending_events.append(
                    SimpleNamespace(
                        service=1,
                        opcode=direct_fixture.GAP_DEVICE_DISCONNECTED,
                        payload=b"",
                    )
                )

            gap.disconnect.side_effect = disconnect
            fixture._session = session
            fixture._gatt = gatt
            fixture._gap = gap
            advertising_calls = 0

            def start_advertising() -> None:
                nonlocal advertising_calls
                advertising_calls += 1
                if advertising_calls < 2 * cycles - 1:
                    pending_events.append(
                        SimpleNamespace(
                            service=1,
                            opcode=direct_fixture.GAP_DEVICE_DISCONNECTED,
                            payload=b"",
                        )
                    )

            fixture._start_advertising = Mock(side_effect=start_advertising)
            fixture.run_recovery_coordination(
                coordination_path, {"updates": 37}, cycles=cycles
            )

            self.assertEqual(gatt.set_value.call_count, cycles)
            self.assertEqual(gatt.get_attributes.call_count, cycles)
            self.assertEqual(gap.disconnect.call_count, cycles)
            self.assertEqual(advertising_calls, 2 * cycles - 1)
            for cycle in range(cycles):
                directory = coordination_path / f"cycle-{cycle}"
                for marker in (
                    "fixture-ready",
                    "value-applied",
                    "fault-triggered",
                    "recovery-ready",
                ):
                    self.assertTrue((directory / marker).is_file())
            self.assertFalse((coordination_path / "cycle-10").exists())


if __name__ == "__main__":
    unittest.main()
