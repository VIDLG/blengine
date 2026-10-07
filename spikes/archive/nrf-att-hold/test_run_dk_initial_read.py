"""Boundary checks for the isolated RN first-read evidence parser."""

from __future__ import annotations

import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_dk_initial_read as diagnostic
from read_hold import ABORT, RELEASE, HoldState, Identity
from run_dk_initial_read import (
    SERVICE,
    buffered_gap_events,
    failed_read_evidence,
    initial_read_evidence,
    new_attempt_paths,
    reconnect_control_session,
    runtime_exit_reason,
    terminal_hold_state,
    verify_new_generation,
)


class InitialReadEvidenceTests(unittest.TestCase):
    def test_requires_one_exact_central_read_marker_after_no_prior_failure(self) -> None:
        line = (
            "BLEHUB_NITRO_RUNTIME rf:step:read-initial:passed:commandId=9010:"
            "connectionId=9007:gattSessionId=9009:"
            f"serviceUuid={SERVICE}:characteristicId=11:value=00"
        )
        self.assertEqual(initial_read_evidence("started\n" + line + "\n"), line)
        for invalid in (
            "started\n",
            line + "\n" + line,
            "BLEHUB_NITRO_RUNTIME rf-io:failed:early\n" + line,
            line.replace("commandId=9010", "commandId=9012"),
            line.replace("connectionId=9007", "connectionId=9006"),
            line.replace("value=00", "value=10"),
            line.replace(SERVICE, "fd000000-0000-4000-8000-000000000000"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                initial_read_evidence(invalid)

    def test_failed_read_requires_matching_discovery_and_permission_terminal(self) -> None:
        discovery = (
            "BLEHUB_NITRO_RUNTIME rf:step:gatt-discovery:passed:"
            f"serviceUuid={SERVICE}:connectionId=9007:gattSessionId=9009"
        )
        failure = (
            "BLEHUB_NITRO_RUNTIME rf:step:read-initial:failed:"
            "commandCompleted.error:9010:permission:ATT authorization rejected"
        )
        self.assertEqual(failed_read_evidence(discovery + "\n" + failure), failure)
        for invalid in (
            failure,
            failure + "\n" + discovery,
            discovery + "\n" + failure.replace("9010", "9012"),
            discovery + "\n" + failure + "\n" + failure,
            discovery + "\n" + failure + "\nBLEHUB_NITRO_RUNTIME rf:step:read-initial:passed:",
            discovery.replace(SERVICE, "fd000000-0000-4000-8000-000000000000") + "\n" + failure,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                failed_read_evidence(invalid)

    def test_terminal_state_must_be_exact_and_bounded(self) -> None:
        class Hold:
            def __init__(self, *states: HoldState) -> None:
                self.states = iter(states)

            def status(self) -> SimpleNamespace:
                return SimpleNamespace(state=next(self.states))

        terminal_hold_state(
            Hold(HoldState.ABORT_REQUESTED, HoldState.ABORTED),
            HoldState.ABORTED,
            time.monotonic() + 1,
        )
        with self.assertRaisesRegex(RuntimeError, "EXPIRED instead of ABORTED"):
            terminal_hold_state(Hold(HoldState.EXPIRED), HoldState.ABORTED, time.monotonic() + 1)
        with self.assertRaises(TimeoutError):
            terminal_hold_state(Hold(), HoldState.EXPIRED, time.monotonic() - 1)

    def test_attempt_logs_are_unique_and_do_not_overwrite_failure_evidence(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(diagnostic, "WORK", Path(directory)),
            patch.object(
                diagnostic.uuid,
                "uuid4",
                side_effect=[SimpleNamespace(hex="a" * 32), SimpleNamespace(hex="b" * 32)],
            ),
        ):
            previous, previous_console = new_attempt_paths("transport-loss")
            previous.write_text("connection failure", encoding="utf-8")
            current, current_console = new_attempt_paths("transport-loss")
            self.assertNotEqual(previous, current)
            self.assertNotEqual(previous_console, current_console)
            self.assertEqual(previous.read_text(encoding="utf-8"), "connection failure")

    def test_buffered_peripheral_events_redact_peer_identity(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.events = iter(
                    (
                        SimpleNamespace(service=diagnostic.GAP, opcode=0x82, payload=b"peer-id"),
                        SimpleNamespace(service=diagnostic.GAP, opcode=0x83, payload=b"peer-id"),
                        SimpleNamespace(service=diagnostic.GATT, opcode=0x82, payload=b"other"),
                        None,
                    )
                )

            def pop_event(self) -> SimpleNamespace | None:
                return next(self.events)

        self.assertEqual(buffered_gap_events(Session()), (1, 1))

    def test_locked_iphone_exit_is_not_misreported_as_a_missing_att_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.log"
            attempt = Path(directory) / "attempt.log"
            console.write_text("BSErrorCodeDescription = Locked\n", encoding="utf-8")
            self.assertIn("unlock it", runtime_exit_reason(console, attempt))
            console.write_text("unexpected application error\n", encoding="utf-8")
            reason = runtime_exit_reason(console, attempt)
            self.assertIn("before WAITING", reason)
            self.assertIn(str(attempt), reason)

    def test_reconnect_never_opens_second_owner_before_old_session_closes(self) -> None:
        calls: list[str] = []

        class OldSession:
            def close(self) -> None:
                calls.append("old-closed")

        class Port:
            def __enter__(self) -> Port:
                calls.append("new-open")
                return self

            def __exit__(self, *_: object) -> None:
                calls.append("new-closed")

        def open_port(**kwargs: object) -> Port:
            self.assertIn("old-closed", calls)
            self.assertEqual(kwargs["port"], "observed-port")
            self.assertTrue(kwargs["exclusive"])
            self.assertTrue(kwargs["rtscts"])
            return Port()

        with (
            patch.object(diagnostic.serial, "Serial", side_effect=open_port),
            contextlib.ExitStack() as owners,
        ):
            session = reconnect_control_session(OldSession(), "observed-port", owners)
            self.assertIsInstance(session, diagnostic.BtpSession)
            self.assertEqual(calls, ["old-closed", "new-open"])
        self.assertEqual(calls, ["old-closed", "new-open", "new-closed"])

        class FailingOldSession:
            def close(self) -> None:
                raise RuntimeError("closing old owner failed")

        with (
            patch.object(diagnostic.serial, "Serial") as serial_open,
            contextlib.ExitStack() as owners,
            self.assertRaisesRegex(RuntimeError, "closing old owner failed"),
        ):
            reconnect_control_session(FailingOldSession(), "observed-port", owners)
        serial_open.assert_not_called()

    def test_stale_controls_cannot_mutate_fresh_arm_and_fresh_arm_is_aborted(self) -> None:
        old = Identity(3, 41)
        fresh = Identity(4, 41)

        class Hold:
            state = HoldState.EXPIRED

            def arm(self, handle: int) -> Identity:
                self.state = HoldState.ARMED
                assert handle == 41
                return fresh

            def status(self) -> SimpleNamespace:
                return SimpleNamespace(state=self.state)

            def abort(self) -> None:
                self.state = HoldState.ABORTED

        class Session:
            def __init__(self) -> None:
                self.opcodes: list[int] = []

            def request(self, *, service: int, opcode: int, payload: bytes) -> SimpleNamespace:
                self.opcodes.append(opcode)
                assert service == 2 and payload == old.encode()
                return SimpleNamespace(opcode=0, payload=b"\x01")

        hold, session = Hold(), Session()
        self.assertEqual(verify_new_generation(session, hold, old), fresh)
        self.assertEqual(session.opcodes, [RELEASE, ABORT])
        self.assertEqual(hold.state, HoldState.ABORTED)


class BtpPortDetectionTests(unittest.TestCase):
    def _detect(
        self,
        responses: dict[str, int | BaseException],
        *,
        changed_identity: bool = False,
    ) -> tuple[str, tuple[int, str, tuple[str, ...]]]:
        ports = ("/dev/cu.usbmodem-alpha", "/dev/cu.usbmodem-beta")
        identity = (0x1061, "2-1.1", ports)
        events: list[str] = []

        class Port:
            def __init__(self, name: str) -> None:
                self.name = name

            def __enter__(self) -> Port:
                self_outer.assertNotIn("open", events[-1:])
                events.append("open")
                return self

            def __exit__(self, *_: object) -> None:
                events.append("close")

        self_outer = self

        def open_port(port: str) -> Port:
            response = responses[port]
            if isinstance(response, diagnostic.serial.SerialException):
                raise response
            return Port(port)

        class Core:
            def __init__(self, connection: Port) -> None:
                self.connection = connection

            def read_supported_services(self) -> int:
                response = responses[self.connection.name]
                if isinstance(response, BaseException):
                    raise response
                return response

        with (
            patch.object(
                diagnostic,
                "verify_probe",
                side_effect=[identity, (0x1061, "3-1", ports)]
                if changed_identity
                else [identity, identity],
            ),
            patch.object(diagnostic, "open_btp_port", side_effect=open_port),
            patch.object(diagnostic, "SerialByteTransport", side_effect=lambda port: port),
            patch.object(diagnostic, "FramedTransport", side_effect=lambda port: port),
            patch.object(diagnostic, "BtpSession", side_effect=lambda port: port),
            patch.object(diagnostic, "CoreCommands", side_effect=Core),
        ):
            try:
                return diagnostic.detect_btp_port("001050252028")
            finally:
                self.assertEqual(events.count("open"), events.count("close"))
                self.assertFalse(
                    any(a == b == "open" for a, b in zip(events, events[1:], strict=False))
                )

    def test_selects_protocol_responder_not_a_vcom_suffix(self) -> None:
        ports = ("/dev/cu.usbmodem-alpha", "/dev/cu.usbmodem-beta")
        required = 1 | (1 << diagnostic.GAP) | (1 << diagnostic.GATT)
        for selected in ports:
            with self.subTest(selected=selected):
                responses = {ports[0]: EOFError(), ports[1]: EOFError()}
                responses[selected] = required
                self.assertEqual(self._detect(responses)[0], selected)

    def test_zero_or_two_responders_fail_closed(self) -> None:
        ports = ("/dev/cu.usbmodem-alpha", "/dev/cu.usbmodem-beta")
        required = 1 | (1 << diagnostic.GAP) | (1 << diagnostic.GATT)
        for responses in (
            {ports[0]: EOFError(), ports[1]: EOFError()},
            {ports[0]: required, ports[1]: required},
            {ports[0]: 1, ports[1]: EOFError()},
        ):
            with (
                self.subTest(responses=responses),
                self.assertRaisesRegex(RuntimeError, "exactly one"),
            ):
                self._detect(responses)

    def test_exclusive_open_failure_is_not_mistaken_for_non_btp_port(self) -> None:
        ports = ("/dev/cu.usbmodem-alpha", "/dev/cu.usbmodem-beta")
        responses = {ports[0]: diagnostic.serial.SerialException("in use"), ports[1]: 15}
        with self.assertRaisesRegex(RuntimeError, "cannot exclusively inspect"):
            self._detect(responses)

    def test_identity_change_after_protocol_probe_fails_closed(self) -> None:
        ports = ("/dev/cu.usbmodem-alpha", "/dev/cu.usbmodem-beta")
        responses = {ports[0]: 15, ports[1]: EOFError()}
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self._detect(responses, changed_identity=True)

    def test_explicit_port_is_only_an_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(diagnostic, "validate_image"),
                patch.object(diagnostic, "APP", root),
                patch.object(diagnostic, "FIXTURE_PID", root / "absent.pid"),
                patch.object(diagnostic, "FIXTURE_WORK", root),
                patch.object(diagnostic, "LOCK", root / "lock"),
                patch.object(
                    diagnostic,
                    "detect_btp_port",
                    return_value=("actual", (0x1061, "2-1.1", ("actual",))),
                ),
                patch.object(diagnostic, "open_btp_port") as open_port,
                self.assertRaisesRegex(RuntimeError, "not the uniquely responding"),
            ):
                diagnostic.run("00008110-000C61160C39401E", "001050252028", "hash", "wrong")
            open_port.assert_not_called()
            self.assertFalse((root / "lock").exists())


if __name__ == "__main__":
    unittest.main()
