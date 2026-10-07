"""Hardware-free ledger and two-generation hold boundary regression tests."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_dk_pending_read_reload as gate
from read_hold import ABORT, RELEASE, HoldError, HoldState, Identity, Snapshot

RUN_ID = "12345678-1234-1234-1234-123456789abc"
PROBE_ID = "abcdefab-cdef-abcd-efab-cdefabcdefab"


def native_marker(
    event: str,
    *,
    run_id: str = RUN_ID,
    probe_id: str = PROBE_ID,
    characteristic: str = "13",
    command: str = "12008",
    connection: str = "12004",
    gatt: str = "12006",
) -> str:
    return (
        f"native:{event}:runId={run_id}:probeId={probe_id}:"
        f"commandId={command}:connectionId={connection}:gattSessionId={gatt}:"
        f"characteristicId={characteristic}"
    )


def evidence() -> str:
    prefix = gate.PREFIX + gate.MODE + ":"
    return "\n".join(
        prefix + line
        for line in (
            f"generation=1:scan:passed:serviceUuid={gate.SERVICE}:peripheralId=0:scanSessionId=12001",
            "generation=1:connected:passed:commandId=12005:connectionId=12004:peripheralId=0",
            "generation=1:gatt:passed:commandId=12007:connectionId=12004:gattSessionId=12006",
            native_marker("read-accepted"),
            "generation=1:read-dispatched:commandId=12008:connectionId=12004:gattSessionId=12006:characteristicId=13:peripheralId=0",
            "external-ready",
            "notification-received",
            "generation=1:cleanup-started",
            "generation=2:listener:registered-before-initialize",
            f"generation=2:scan:passed:serviceUuid={gate.SERVICE}:peripheralId=1:scanSessionId=13001",
            "generation=2:connected:passed:commandId=13005:connectionId=13004:peripheralId=1",
            "generation=2:gatt:passed:commandId=13007:connectionId=13004:gattSessionId=13006",
            native_marker("dispose-returned"),
            native_marker("late-submit-rejected"),
            "generation=2:awaiting-arm:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=17",
            "arm-ready",
            "arm-notification-received",
            "generation=2:read-dispatched:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=17",
            "generation=2:read:passed:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=17:value=00",
            "complete:passed",
        )
    )


class LedgerTests(unittest.TestCase):
    def test_exact_ordered_two_generation_terminal_and_service(self) -> None:
        text = evidence()
        self.assertEqual(
            gate.verify_ledger(
                text,
                run_id=RUN_ID,
                post_steps=("reload", "arm"),
                old_characteristic="13",
            ),
            ("13", "17"),
        )
        lines = text.splitlines()

        def swapped(first: str, second: str) -> str:
            entries = lines.copy()
            a = next(i for i, line in enumerate(entries) if line.endswith(first))
            b = next(i for i, line in enumerate(entries) if line.endswith(second))
            entries[a], entries[b] = entries[b], entries[a]
            return "\n".join(entries)

        for invalid in (
            "\n".join(lines[:-1]),
            text + "\n" + lines[-2],
            text + "\n" + lines[3],
            text
            + "\n"
            + gate.PREFIX
            + gate.MODE
            + ":generation=1:read-dispatched:commandId=999",
            text
            + "\n"
            + gate.PREFIX
            + gate.MODE
            + ":generation=2:read:passed:commandId=999",
            text.replace("commandId=13008", "commandId=12008"),
            text.replace("connectionId=13004", "connectionId=12004"),
            text.replace("commandId=13005", "commandId=12005"),
            text.replace("peripheralId=1", "peripheralId=2", 1),
            text.replace("gattSessionId=13006", "gattSessionId=12006"),
            text.replace("value=00", "value=01"),
            text.replace(gate.SERVICE, "fd000000-0000-4000-8000-000000000000"),
            text.replace(gate.PREFIX + gate.MODE + ":notification-received\n", ""),
            text.replace(gate.PREFIX + gate.MODE + ":arm-notification-received\n", ""),
            text.replace("arm-ready\n", ""),
            swapped(
                "generation=1:cleanup-started",
                "generation=2:listener:registered-before-initialize",
            ),
            swapped("arm-ready", "arm-notification-received"),
            swapped(
                "arm-notification-received",
                "generation=2:read-dispatched:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=17",
            ),
            text + "\n" + gate.PREFIX + gate.MODE + ":arm-notification-received",
            text
            + "\n"
            + gate.PREFIX
            + gate.MODE
            + ":generation=2:late:event:commandId=12008",
            text.replace(
                "generation=2:awaiting-arm:commandId=13008",
                "generation=2:awaiting-arm:commandId=12008",
            ),
            text.replace(
                "generation=2:read-dispatched:commandId=13008",
                "generation=2:read-dispatched:commandId=12008",
            ),
            text.replace(
                "generation=2:read-dispatched:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=17",
                "generation=2:read-dispatched:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=18",
            ),
            text.replace("generation=1:cleanup-started\n", ""),
            text.replace("generation=2:listener:registered-before-initialize\n", ""),
            text.replace("external-ready\n", ""),
            text.replace(
                "external-ready\n" + gate.PREFIX + gate.MODE + ":notification-received",
                "notification-received\n" + gate.PREFIX + gate.MODE + ":external-ready",
            ),
            text
            + "\n"
            + gate.PREFIX
            + gate.MODE
            + ":generation=1:read:passed:commandId=12008",
            text
            + "\n"
            + gate.PREFIX
            + gate.MODE
            + ":generation=1:connected:passed:connectionId=12004",
            text + "\n" + gate.PREFIX + gate.MODE + ":generation=2:failed:timeout",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                gate.verify_ledger(invalid, run_id=RUN_ID, post_steps=("reload", "arm"))
        for posts in ((), ("reload",), ("arm", "reload"), ("reload", "arm", "arm")):
            with (
                self.subTest(posts=posts),
                self.assertRaisesRegex(RuntimeError, "two ordered"),
            ):
                gate.verify_ledger(text, run_id=RUN_ID, post_steps=posts)
        with self.assertRaisesRegex(RuntimeError, "characteristic changed"):
            gate.verify_ledger(
                text,
                run_id=RUN_ID,
                post_steps=("reload", "arm"),
                old_characteristic="99",
            )

    def test_native_boundary_table_rejects_identity_order_and_failure_variants(
        self,
    ) -> None:
        text = evidence()
        prefix = gate.PREFIX + gate.MODE + ":"
        native_lines = {
            event: prefix + native_marker(event)
            for event in ("read-accepted", "dispose-returned", "late-submit-rejected")
        }

        def swapped(first: str, second: str) -> str:
            lines = text.splitlines()
            first_index = next(
                i for i, line in enumerate(lines) if line.endswith(first)
            )
            second_index = next(
                i for i, line in enumerate(lines) if line.endswith(second)
            )
            lines[first_index], lines[second_index] = (
                lines[second_index],
                lines[first_index],
            )
            return "\n".join(lines)

        cases = (
            (
                "missing read accepted",
                text.replace(native_lines["read-accepted"] + "\n", "", 1),
            ),
            (
                "missing dispose",
                text.replace(native_lines["dispose-returned"] + "\n", "", 1),
            ),
            (
                "missing late submit",
                text.replace(native_lines["late-submit-rejected"] + "\n", "", 1),
            ),
            ("duplicate read accepted", text + "\n" + native_lines["read-accepted"]),
            ("duplicate dispose", text + "\n" + native_lines["dispose-returned"]),
            (
                "duplicate late submit",
                text + "\n" + native_lines["late-submit-rejected"],
            ),
            (
                "cross run",
                text.replace(
                    f"runId={RUN_ID}",
                    "runId=87654321-4321-4321-4321-cba987654321",
                    1,
                ),
            ),
            (
                "cross probe",
                text.replace(
                    f"probeId={PROBE_ID}",
                    "probeId=01234567-89ab-cdef-0123-456789abcdef",
                    1,
                ),
            ),
            (
                "mismatched command",
                text.replace(
                    native_lines["read-accepted"],
                    prefix + native_marker("read-accepted", command="12009"),
                    1,
                ),
            ),
            (
                "mismatched connection",
                text.replace(
                    native_lines["read-accepted"],
                    prefix + native_marker("read-accepted", connection="12005"),
                    1,
                ),
            ),
            (
                "mismatched gatt session",
                text.replace(
                    native_lines["read-accepted"],
                    prefix + native_marker("read-accepted", gatt="12007"),
                    1,
                ),
            ),
            (
                "mismatched characteristic",
                text.replace(
                    native_lines["read-accepted"],
                    prefix + native_marker("read-accepted", characteristic="14"),
                    1,
                ),
            ),
            (
                "read accepted reordered after old dispatch",
                swapped(
                    native_marker("read-accepted"),
                    "generation=1:read-dispatched:commandId=12008:connectionId=12004:gattSessionId=12006:characteristicId=13:peripheralId=0",
                ),
            ),
            (
                "dispose and late reversed",
                swapped(
                    native_marker("dispose-returned"),
                    native_marker("late-submit-rejected"),
                ),
            ),
            (
                "dispose before old cleanup",
                swapped(
                    native_marker("dispose-returned"), "generation=1:cleanup-started"
                ),
            ),
            (
                "dispose failed",
                text + "\n" + prefix + "failed:native-boundary-dispose-failed",
            ),
            (
                "unknown native marker",
                text + "\n" + prefix + "native:unknown:runId=" + RUN_ID,
            ),
        )
        for label, invalid in cases:
            with self.subTest(label=label), self.assertRaises(RuntimeError):
                gate.verify_ledger(invalid, run_id=RUN_ID, post_steps=("reload", "arm"))

    def test_notification_posts_enforce_native_boundaries_before_dispatch(self) -> None:
        prefix = gate.PREFIX + gate.MODE + ":"
        process = SimpleNamespace(poll=lambda: None, returncode=None)
        lines = evidence().splitlines()
        external_ready_index = next(
            i for i, line in enumerate(lines) if line.endswith("external-ready")
        )
        arm_ready_index = next(
            i for i, line in enumerate(lines) if line.endswith("arm-ready")
        )
        reload_text = "\n".join(lines[: external_ready_index + 1])
        arm_text = "\n".join(lines[: arm_ready_index + 1])
        old_line = next(
            line
            for line in lines
            if line.endswith(
                "generation=1:read-dispatched:commandId=12008:connectionId=12004:"
                "gattSessionId=12006:characteristicId=13:peripheralId=0"
            )
        )
        accepted_line = prefix + native_marker("read-accepted")
        dispose_line = prefix + native_marker("dispose-returned")
        late_line = prefix + native_marker("late-submit-rejected")

        def swapped(text: str, first: str, second: str) -> str:
            entries = text.splitlines()
            first_index = next(i for i, line in enumerate(entries) if line == first)
            second_index = next(i for i, line in enumerate(entries) if line == second)
            entries[first_index], entries[second_index] = (
                entries[second_index],
                entries[first_index],
            )
            return "\n".join(entries)

        reload_cases = (
            ("missing accepted", reload_text.replace(accepted_line + "\n", "", 1)),
            (
                "accepted after old dispatch",
                swapped(reload_text, accepted_line, old_line),
            ),
            (
                "accepted cross run",
                reload_text.replace(
                    f"runId={RUN_ID}",
                    "runId=87654321-4321-4321-4321-cba987654321",
                    1,
                ),
            ),
        )
        arm_cases = (
            ("missing dispose", arm_text.replace(dispose_line + "\n", "", 1)),
            ("duplicate late", arm_text + "\n" + late_line),
            (
                "cross probe",
                arm_text.replace(
                    f"probeId={PROBE_ID}",
                    "probeId=01234567-89ab-cdef-0123-456789abcdef",
                    1,
                ),
            ),
            (
                "mismatched characteristic",
                arm_text.replace(
                    accepted_line,
                    prefix + native_marker("read-accepted", characteristic="14"),
                    1,
                ),
            ),
            (
                "dispose failed",
                arm_text + "\n" + prefix + "failed:native-boundary-dispose-failed",
            ),
            ("dispose and late reversed", swapped(arm_text, dispose_line, late_line)),
            (
                "late after arm ready",
                swapped(arm_text, late_line, prefix + "arm-ready"),
            ),
        )
        for stage, cases in (("reload", reload_cases), ("arm", arm_cases)):
            for label, invalid in cases:
                with (
                    self.subTest(stage=stage, label=label),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    log = Path(directory) / "attempt.log"
                    log.write_text(invalid, encoding="utf-8")
                    with patch.object(gate.subprocess, "run") as post:
                        with self.assertRaises(RuntimeError):
                            gate.post_notification(
                                "xcrun",
                                "device",
                                "same-name",
                                log,
                                process,
                                stage,
                                time.monotonic() + 1,
                                run_id=RUN_ID,
                            )
                        post.assert_not_called()

    def test_two_posts_use_same_name_only_at_their_respective_ready_boundaries(
        self,
    ) -> None:
        name = "dev.blehub.rn.reload.12345678-1234-1234-1234-123456789abc"
        prefix = gate.PREFIX + gate.MODE + ":"
        commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            old = (
                prefix
                + native_marker("read-accepted")
                + "\n"
                + prefix
                + gate.OLD.replace("([1-9][0-9]*)", "13").replace("([0-9]+)", "0")
                + "\n"
            )
            log.write_text(old + prefix + "external-ready\n", encoding="utf-8")
            process = SimpleNamespace(poll=lambda: None, returncode=None)

            def fake_post(command: list[str], **kwargs: object) -> SimpleNamespace:
                commands.append(command)
                marker = (
                    "notification-received"
                    if len(commands) == 1
                    else "arm-notification-received"
                )
                with log.open("a", encoding="utf-8") as output:
                    output.write(prefix + marker + "\n")
                return SimpleNamespace(returncode=0)

            with patch.object(gate.subprocess, "run", side_effect=fake_post):
                with self.assertRaisesRegex(RuntimeError, "first-post count"):
                    gate.post_notification(
                        "xcrun",
                        "device",
                        name,
                        log,
                        process,
                        "arm",
                        time.monotonic() + 1,
                        run_id=RUN_ID,
                    )
                gate.post_notification(
                    "xcrun",
                    "device",
                    name,
                    log,
                    process,
                    "reload",
                    time.monotonic() + 1,
                    run_id=RUN_ID,
                )
                with self.assertRaisesRegex(RuntimeError, "first-post count"):
                    gate.post_notification(
                        "xcrun",
                        "device",
                        name,
                        log,
                        process,
                        "reload",
                        time.monotonic() + 1,
                        run_id=RUN_ID,
                    )
                with log.open("a", encoding="utf-8") as output:
                    output.write(prefix + "generation=1:cleanup-started\n")
                    output.write(prefix + native_marker("dispose-returned") + "\n")
                    output.write(prefix + native_marker("late-submit-rejected") + "\n")
                    output.write(
                        prefix + gate.AWAITING_ARM.replace("([1-9][0-9]*)", "17") + "\n"
                    )
                    output.write(prefix + "arm-ready\n")
                gate.post_notification(
                    "xcrun",
                    "device",
                    name,
                    log,
                    process,
                    "arm",
                    time.monotonic() + 1,
                    run_id=RUN_ID,
                )
                with self.assertRaisesRegex(RuntimeError, "unique ready boundary"):
                    gate.post_notification(
                        "xcrun",
                        "device",
                        name,
                        log,
                        process,
                        "arm",
                        time.monotonic() + 1,
                        run_id=RUN_ID,
                    )
            self.assertEqual(len(commands), 2)
            self.assertEqual(
                [command[command.index("--name") + 1] for command in commands],
                [name, name],
            )
            self.assertEqual(
                gate.markers(log.read_text(encoding="utf-8")).count(
                    gate.MODE + ":arm-notification-received"
                ),
                1,
            )

    def test_early_arm_receipt_duplicate_and_deadline_prevent_post(self) -> None:
        prefix = gate.PREFIX + gate.MODE + ":"
        process = SimpleNamespace(poll=lambda: None, returncode=None)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            for content in (
                "arm-ready\n",
                "notification-received\narm-ready\narm-notification-received\n",
                "notification-received\narm-ready\ngeneration=2:read-dispatched:early\n",
            ):
                log.write_text(
                    "".join(prefix + marker + "\n" for marker in content.splitlines()),
                    encoding="utf-8",
                )
                with (
                    patch.object(gate.subprocess, "run") as post,
                    self.assertRaises(RuntimeError),
                ):
                    gate.post_notification(
                        "xcrun",
                        "device",
                        "same-name",
                        log,
                        process,
                        "arm",
                        time.monotonic() + 1,
                        run_id=RUN_ID,
                    )
                post.assert_not_called()
            old = (
                prefix
                + native_marker("read-accepted")
                + "\n"
                + prefix
                + gate.OLD.replace("([1-9][0-9]*)", "13").replace("([0-9]+)", "0")
                + "\n"
            )
            log.write_text(old + prefix + "external-ready\n", encoding="utf-8")
            with (
                patch.object(gate.subprocess, "run") as post,
                self.assertRaisesRegex(TimeoutError, "ready deadline"),
            ):
                gate.post_notification(
                    "xcrun",
                    "device",
                    "same-name",
                    log,
                    process,
                    "reload",
                    time.monotonic() - 1,
                    run_id=RUN_ID,
                )
            post.assert_not_called()

    def test_early_failure_or_exit_cannot_be_treated_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            process = SimpleNamespace(poll=lambda: None, returncode=None)
            log.write_text(
                gate.PREFIX + gate.MODE + ":generation=1:failed:dispatch",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "RN reported a failure"):
                gate.wait_marker(
                    log, process, gate.MODE + ":external-ready", time.monotonic() + 1
                )
            log.write_text("", encoding="utf-8")
            process = SimpleNamespace(poll=lambda: 0, returncode=0)
            with self.assertRaisesRegex(RuntimeError, "exited before"):
                gate.wait_marker(
                    log, process, gate.MODE + ":external-ready", time.monotonic() + 1
                )


class FirmwareBoundaryTests(unittest.TestCase):
    def test_expiry_requires_same_peer_disconnect_and_exact_state(self) -> None:
        peer = bytes(range(7))

        class Session:
            def __init__(self, events: list[tuple[int, bytes]]) -> None:
                self.events = iter(
                    [
                        SimpleNamespace(
                            service=gate.GAP, opcode=opcode, payload=address
                        )
                        for opcode, address in events
                    ]
                    + [None]
                )

            def pop_event(self) -> SimpleNamespace | None:
                return next(self.events, None)

        class Hold:
            def __init__(self, *states: HoldState) -> None:
                self.states = iter(states)

            def status(self) -> SimpleNamespace:
                return SimpleNamespace(state=next(self.states))

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            log.write_text("", encoding="utf-8")
            process = SimpleNamespace(poll=lambda: None, returncode=None)
            gate.wait_expired_and_disconnected(
                Hold(HoldState.WAITING, HoldState.EXPIRED),
                Session([(gate.GAP_DEVICE_DISCONNECTED, peer)]),
                peer,
                time.monotonic(),
                process,
                log,
            )
            for events, state in (
                ([(gate.GAP_DEVICE_DISCONNECTED, b"different")], HoldState.EXPIRED),
                ([(gate.GAP_DEVICE_DISCONNECTED, peer)] * 2, HoldState.EXPIRED),
                ([(gate.GAP_DEVICE_CONNECTED, peer)], HoldState.EXPIRED),
                ([], HoldState.ABORTED),
            ):
                with (
                    self.subTest(events=events, state=state),
                    self.assertRaises(RuntimeError),
                ):
                    gate.wait_expired_and_disconnected(
                        Hold(state),
                        Session(events),
                        peer,
                        time.monotonic(),
                        process,
                        log,
                    )
            with self.assertRaises(TimeoutError):
                gate.wait_expired_and_disconnected(
                    Hold(),
                    Session([]),
                    peer,
                    time.monotonic() - gate.HOLD_SECONDS - 3,
                    process,
                    log,
                )

    def test_stale_release_and_abort_must_both_reject_without_changing_fresh_arm(
        self,
    ) -> None:
        old, fresh = Identity(12, 41), Identity(13, 41)

        class Session:
            def __init__(self, fail_opcode: int | None = None) -> None:
                self.calls: list[int] = []
                self.fail_opcode = fail_opcode

            def request(
                self, *, service: int, opcode: int, payload: bytes
            ) -> SimpleNamespace:
                self.calls.append(opcode)
                assert service == gate.GATT_SERVICE and payload == old.encode()
                return SimpleNamespace(
                    service=service,
                    index=0,
                    opcode=opcode if opcode == self.fail_opcode else 0,
                    payload=b"\x01",
                )

        hold = SimpleNamespace(status=lambda: SimpleNamespace(state=HoldState.ARMED))
        session = Session()
        gate.reject_stale_controls(session, hold, old, fresh)
        self.assertEqual(session.calls, [RELEASE, ABORT])
        for bad in (old, Identity(13, 42)):
            with self.assertRaisesRegex(RuntimeError, "fresh hold"):
                gate.reject_stale_controls(Session(), hold, old, bad)
        with self.assertRaisesRegex(RuntimeError, "did not reject"):
            gate.reject_stale_controls(Session(ABORT), hold, old, fresh)
        with self.assertRaisesRegex(RuntimeError, "changed or raced"):
            gate.reject_stale_controls(
                Session(),
                SimpleNamespace(
                    status=lambda: SimpleNamespace(state=HoldState.WAITING)
                ),
                old,
                fresh,
            )

    def test_fresh_wait_allows_bounded_native_latency_and_reports_no_entry(
        self,
    ) -> None:
        fresh = Identity(13, 41)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            log.write_text(
                gate.PREFIX
                + gate.MODE
                + ":generation=2:read-dispatched:commandId=13008:connectionId=13004:gattSessionId=13006:characteristicId=17\n",
                encoding="utf-8",
            )
            process = SimpleNamespace(poll=lambda: None, returncode=None)

            class Hold:
                def wait_until_held(self, deadline: float) -> Identity:
                    self_deadline = deadline - time.monotonic()
                    assert self_deadline > 10, (
                        "native latency budget regressed to two seconds"
                    )
                    return fresh

            self.assertLessEqual(
                gate.confirm_fresh_waiting(
                    Hold(),
                    fresh,
                    log,
                    process,
                    time.monotonic() + gate.FRESH_READ_TIMEOUT_SECONDS,
                ),
                time.monotonic(),
            )
            with self.assertRaisesRegex(
                RuntimeError, "did not enter WAITING within 12s"
            ):
                gate.confirm_fresh_waiting(
                    SimpleNamespace(
                        wait_until_held=lambda _: (_ for _ in ()).throw(
                            HoldError("deadline")
                        )
                    ),
                    fresh,
                    log,
                    process,
                    time.monotonic() + gate.FRESH_READ_TIMEOUT_SECONDS,
                )
            log.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "did not enter WAITING within 12s"
            ):
                gate.confirm_fresh_waiting(
                    Hold(), fresh, log, process, time.monotonic() + 0.01
                )

    def test_fresh_release_checks_elapsed_time_after_native_status(self) -> None:
        fresh = Identity(13, 41)
        calls: list[str] = []
        hold = SimpleNamespace(
            status=lambda: Snapshot(HoldState.WAITING, fresh),
            release=lambda: calls.append("release"),
        )
        with patch.object(gate, "terminal_hold_state") as terminal:
            gate.release_fresh(hold, fresh, time.monotonic())
            terminal.assert_called_once()
        self.assertEqual(calls, ["release"])
        calls.clear()
        with self.assertRaisesRegex(TimeoutError, "started too late"):
            gate.release_fresh(hold, fresh, time.monotonic() - 3.6)
        self.assertEqual(calls, [])
        start = time.monotonic()
        with (
            patch.object(
                gate.time, "monotonic", side_effect=[start + 0.1, start + 3.6]
            ),
            self.assertRaisesRegex(TimeoutError, "took too long"),
        ):
            gate.release_fresh(hold, fresh, start)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(RuntimeError, "exact WAITING"):
            gate.release_fresh(
                SimpleNamespace(
                    status=lambda: Snapshot(HoldState.EXPIRED, fresh),
                    release=lambda: calls.append("release"),
                ),
                fresh,
                time.monotonic(),
            )
        self.assertEqual(calls, [])

    def test_lock_is_not_stolen_and_bad_port_releases_only_owned_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.mkdir()
            lock = fixture / "scan.lock"
            with (
                patch.object(gate, "validate_image"),
                patch.object(gate, "APP", root),
                patch.object(gate, "FIXTURE_PID", fixture / "fixture.pid"),
                patch.object(gate, "FIXTURE_WORK", fixture),
                patch.object(gate, "LOCK", lock),
                patch.object(
                    gate,
                    "detect_btp_port",
                    return_value=("actual", (1, "usb", ("actual",))),
                ),
            ):
                lock.mkdir()
                with self.assertRaises(FileExistsError):
                    gate.run("00008110-000C61160C39401E", "probe", "sha")
                self.assertTrue(lock.is_dir())
                lock.rmdir()
                with self.assertRaisesRegex(RuntimeError, "uniquely responding"):
                    gate.run("00008110-000C61160C39401E", "probe", "sha", "wrong")
                self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
