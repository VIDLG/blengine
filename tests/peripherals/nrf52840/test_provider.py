from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tests.peripherals.nrf52840 import direct_fixture, fixture_session, provider
from tests.peripherals.nrf52840.resource_lease import ResourceLeaseError


class ProviderRegressionTests(unittest.TestCase):
    def test_transitional_start_cannot_reuse_or_launch_unattested_firmware(
        self,
    ) -> None:
        profile = json.loads(provider.PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(profile["profile_id"], "blehub-nrf52840-multifunction-v2-a")
        with (
            patch.object(provider, "_process_identity") as identity,
            patch.object(provider, "_nrftest_root") as root,
            patch.object(provider.subprocess, "Popen") as launch,
            self.assertRaisesRegex(
                provider.Nrf52840Error, "immutable v2.*no v1 fallback"
            ),
        ):
            provider.start(ready_timeout=1, rf_timeout=10)
        identity.assert_not_called()
        root.assert_not_called()
        launch.assert_not_called()

    def test_attested_direct_start_rejects_running_fixture_without_reusing_proof(
        self,
    ) -> None:
        with (
            patch.object(provider, "_process_identity", return_value=(43210, 43210)),
            patch.object(
                provider, "_fixture_state", return_value=provider._PROCESS_RUNNING
            ),
            patch.object(provider, "_wait_ready") as ready,
            patch.object(provider.subprocess, "Popen") as launch,
            patch.object(provider, "stop") as stop,
            self.assertRaisesRegex(
                provider.Nrf52840Error, "cannot reuse.*live-attested"
            ),
        ):
            provider.direct_start(fixture_request=Path("request.json"))
        ready.assert_not_called()
        launch.assert_not_called()
        stop.assert_not_called()

    def test_direct_start_forwards_request_to_fresh_child_with_coordination(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request = Path(temporary) / "session" / "request.json"
            coordination = str(Path(temporary) / "coordination")
            with (
                patch.object(provider, "WORK_ROOT", Path(temporary)),
                patch.object(provider, "PID_FILE", Path(temporary) / "fixture.pid"),
                patch.object(provider, "LOG_FILE", Path(temporary) / "fixture.log"),
                patch.object(provider, "_process_identity", return_value=None),
                patch.object(provider, "_port", return_value="test-port"),
                patch.object(
                    provider.subprocess,
                    "Popen",
                    return_value=SimpleNamespace(pid=43210),
                ) as launch,
                patch.object(provider.os, "getpgid", return_value=43210),
                patch.object(provider, "_wait_ready") as ready,
                redirect_stdout(io.StringIO()),
            ):
                provider.direct_start(
                    coordination, "notification", fixture_request=request
                )
            self.assertEqual(
                launch.call_args.args[0],
                [
                    provider.sys.executable,
                    "-u",
                    "-m",
                    "tests.peripherals.nrf52840.provider",
                    "direct-serve",
                    "--port",
                    "test-port",
                    "--fixture-request",
                    str(request.resolve()),
                    "--coordination-path",
                    coordination,
                    "--coordination-mode",
                    "notification",
                ],
            )
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            ready.assert_called_once_with(
                43210,
                provider.DEFAULT_READY_TIMEOUT,
                "NRF52840_DIRECT_READY",
                process=launch.return_value,
            )

    def test_direct_serve_loads_request_and_uses_only_snapshot_profile(self) -> None:
        request = Path("request.json")
        snapshot = Path("session/profile.json")
        plan = SimpleNamespace(
            profile_path=snapshot, port="test-port", resource_identity="jlink:123456"
        )
        load = Mock(return_value=plan)
        with (
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(fixture_session, "load_session", load),
            patch.object(provider, "serve") as serve,
        ):
            provider.direct_serve(
                "coordination", "notification", fixture_request=request
            )
        load.assert_called_once_with(request)
        serve.assert_called_once_with(
            "test-port",
            snapshot,
            Path("coordination"),
            "notification",
            fixture_session=plan,
        )
        self.assertNotEqual(snapshot, provider.PROFILE)

    def test_invalid_request_does_not_start_fixture(self) -> None:
        load = Mock(side_effect=ValueError("snapshot hash mismatch"))
        with (
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(fixture_session, "load_session", load),
            patch.object(provider, "serve") as serve,
            self.assertRaisesRegex(ValueError, "snapshot hash mismatch"),
        ):
            provider.direct_serve(fixture_request=Path("request.json"))
        serve.assert_not_called()

    def test_no_request_direct_serve_refuses_missing_export_before_port_or_child(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                provider, "EXPECTED_CONTRACT", Path(directory) / "missing-v2.json"
            ),
            patch.object(provider, "_port") as port,
            patch.object(provider, "serve") as serve,
        ):
            with self.assertRaisesRegex(
                ValueError, "missing fixture contract.*build/export"
            ):
                provider.direct_serve()
            port.assert_not_called()
            serve.assert_not_called()

    def test_cli_forwards_optional_fixture_request_for_both_direct_commands(
        self,
    ) -> None:
        for command, request, port in (
            ("direct-start", None, None),
            ("direct-start", Path("session/request.json"), "test-port"),
            ("direct-serve", None, None),
            ("direct-serve", Path("session/request.json"), "test-port"),
        ):
            arguments = [
                "nrf52840-provider",
                command,
                "--coordination-mode",
                "notification",
            ]
            if request is not None:
                arguments.extend(("--fixture-request", str(request)))
            if port is not None:
                arguments.extend(("--port", port))
            with (
                self.subTest(command=command, request=request),
                patch.object(provider.sys, "argv", arguments),
                patch.object(provider, command.replace("-", "_")) as run,
            ):
                self.assertEqual(provider.main(), 0)
            run.assert_called_once_with(
                None, "notification", fixture_request=request, port=port
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
            provider.direct_start(
                str(Path(temporary) / "coordination"),
                "recovery-ios",
                fixture_request=Path(temporary) / "request.json",
            )

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
            patch.object(
                provider, "EXPECTED_CONTRACT", Path(temporary) / "missing-v2.json"
            ),
            self.assertRaisesRegex(ValueError, "missing fixture contract"),
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
            patch.object(provider, "port_lease") as lease,
            patch.object(provider, "open_serial", return_value=serial),
            patch.object(provider, "run_profile_doctor", return_value=result) as doctor,
            patch.object(provider, "BtpSession") as session,
            patch.object(provider, "FramedTransport") as framed,
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.doctor()
        lease.assert_called_once_with("test-port", purpose="BleHub nRF52840 doctor")
        doctor.assert_called_once_with(session.return_value, provider.PROFILE)
        session.return_value.close.assert_called_once_with()
        serial.close.assert_called_once_with()
        framed.assert_called_once_with(serial)
        session.assert_called_once_with(framed.return_value)
        self.assertIn("nrf52840_doctor=passed", output.getvalue())

    def test_doctor_lease_conflict_prevents_serial_open(self) -> None:
        @contextmanager
        def conflict(*args: object, **kwargs: object) -> Iterator[None]:
            raise ResourceLeaseError("fixture already leased")
            yield

        with (
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(provider, "port_lease", side_effect=conflict),
            patch.object(provider, "open_serial") as open_serial,
            patch.object(provider, "BtpSession") as session,
            patch.object(provider, "run_profile_doctor") as doctor,
            self.assertRaisesRegex(provider.Nrf52840Error, "fixture already leased"),
        ):
            provider.doctor()
        open_serial.assert_not_called()
        session.assert_not_called()
        doctor.assert_not_called()

    def test_doctor_keeps_lease_through_cleanup_even_on_failure(self) -> None:
        for failure in (
            None,
            "open",
            "transport",
            "session",
            "doctor",
            "session-close",
            "serial-close",
        ):
            with self.subTest(failure=failure):
                events: list[str] = []

                @contextmanager
                def lease(
                    *args: object, events: list[str] = events, **kwargs: object
                ) -> Iterator[None]:
                    events.append("acquire")
                    try:
                        yield
                    finally:
                        events.append("release")

                def step(
                    name: str,
                    result: object = None,
                    *,
                    events: list[str] = events,
                    failure: str | None = failure,
                ) -> object:
                    self.assertEqual(events[0], "acquire")
                    self.assertNotIn("release", events)
                    events.append(name)
                    if name == failure:
                        raise RuntimeError(f"failed {name}")
                    return result

                serial = Mock()
                serial.close.side_effect = lambda: step("serial-close")
                session = Mock()
                session.close.side_effect = lambda: step("session-close")
                result = SimpleNamespace(
                    profile_id="test", supported_services=6, role_handles={}
                )
                with (
                    patch.object(provider, "_port", return_value="test-port"),
                    patch.object(provider, "port_lease", side_effect=lease),
                    patch.object(
                        provider,
                        "open_serial",
                        side_effect=lambda _, serial=serial: step("open", serial),
                    ),
                    patch.object(
                        provider,
                        "FramedTransport",
                        side_effect=lambda _: step("transport"),
                    ),
                    patch.object(
                        provider,
                        "BtpSession",
                        side_effect=lambda _, session=session: step("session", session),
                    ),
                    patch.object(
                        provider,
                        "run_profile_doctor",
                        side_effect=lambda *_, result=result: step("doctor", result),
                    ),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    if failure is None:
                        provider.doctor()
                        self.assertIn("nrf52840_doctor=passed", output.getvalue())
                    else:
                        with self.assertRaisesRegex(
                            provider.Nrf52840Error, f"failed {failure}"
                        ):
                            provider.doctor()
                        self.assertNotIn("nrf52840_doctor=passed", output.getvalue())
                self.assertEqual(events[-1], "release")
                if failure != "open":
                    serial.close.assert_called_once_with()
                    self.assertEqual(events[-2], "serial-close")
                if failure not in ("open", "transport", "session"):
                    session.close.assert_called_once_with()
                    self.assertEqual(events[-3], "session-close")

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

    def test_owned_child_exit_precedes_liveness_and_stale_ready(self) -> None:
        for returncode in (0, 7):
            for marker in (provider.READY_MARKER, "NRF52840_DIRECT_READY"):
                with self.subTest(returncode=returncode, marker=marker):
                    process = Mock(pid=43210)
                    process.poll.return_value = returncode
                    with tempfile.TemporaryDirectory() as temporary:
                        log = Path(temporary) / "fixture.log"
                        log.write_text(
                            marker + "old-run\nchild failure detail\n", encoding="utf-8"
                        )
                        with (
                            patch.object(provider, "LOG_FILE", log),
                            patch.object(
                                provider, "_alive", return_value=True
                            ) as alive,
                            patch.object(provider.time, "sleep") as sleep,
                            redirect_stdout(io.StringIO()) as output,
                            self.assertRaisesRegex(
                                provider.Nrf52840Error,
                                r"exited before readiness:[\s\S]*child failure detail",
                            ),
                        ):
                            provider._wait_ready(
                                process.pid, 45, marker, process=process
                            )
                        process.poll.assert_called_once_with()
                        alive.assert_not_called()
                        sleep.assert_not_called()
                        self.assertNotIn("nrf52840_fixture=ready", output.getvalue())

    def test_live_owned_child_polls_before_accepting_readiness(self) -> None:
        process = Mock(pid=43210)
        process.poll.return_value = None

        def alive(pid: int) -> bool:
            self.assertEqual(pid, process.pid)
            process.poll.assert_called_once_with()
            return True

        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "fixture.log"
            log.write_text("NRF52840_DIRECT_READY\n", encoding="utf-8")
            with (
                patch.object(provider, "LOG_FILE", log),
                patch.object(provider, "_alive", side_effect=alive),
                patch.object(provider.time, "sleep") as sleep,
                redirect_stdout(io.StringIO()) as output,
            ):
                provider._wait_ready(
                    process.pid, 45, "NRF52840_DIRECT_READY", process=process
                )
            sleep.assert_not_called()
            self.assertIn("nrf52840_fixture=ready", output.getvalue())

    def test_real_unreaped_child_fails_promptly_and_stop_cleans_identity(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "PID_FILE", Path(temporary) / "fixture.pid"),
            patch.object(provider, "LOG_FILE", Path(temporary) / "fixture.log"),
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; sys.stdin.read(); "
                        "print('child failure detail'); sys.exit(7)"
                    ),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            ) as process,
        ):
            try:
                identity = provider._write_process_identity(process.pid)
                assert process.stdin is not None
                assert process.stdout is not None
                process.stdin.close()
                # EOF observes child shutdown without wait()/communicate() reaping it.
                provider.LOG_FILE.write_text(process.stdout.read(), encoding="utf-8")
                self.assertIsNone(process.returncode)
                self.assertTrue(provider._alive(process.pid))
                started = time.monotonic()
                with self.assertRaisesRegex(
                    provider.Nrf52840Error,
                    r"exited before readiness:[\s\S]*child failure detail",
                ):
                    provider._wait_ready(process.pid, 3, process=process)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(process.returncode, 7)
                self.assertFalse(provider._alive(process.pid))
                with redirect_stdout(io.StringIO()):
                    provider.stop(expected_identity=identity)
                self.assertFalse(provider.PID_FILE.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

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
