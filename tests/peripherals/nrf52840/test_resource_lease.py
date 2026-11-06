"""Hardware-free lifecycle tests using nrftest's real child-process lock authority.

The checkout comes from the existing local configuration/environment. Only child
processes import nrftest; all resource keys below are synthetic and all lock files
live in a temporary directory, never in the user's production lock namespace.
Cross-repository tests explicitly skip when no configured checkout is available.
POSIX lifecycle results do not establish Windows Pixi descendant cleanup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from serial.tools import list_ports

from . import direct_fixture, resource_lease

# Fault modes still own a real nrftest lease, so reacquisition proves rollback.
BROKER_SOURCE = r"""
import json
import sys
import threading
from pathlib import Path
from host.nrftest import resource_lease as authority

lock_root, mode, kind, serial, purpose = sys.argv[1:]
authority.RESOURCE_LOCK_ROOT = Path(lock_root)
if mode == "real":
    sys.argv = ["broker", "--kind", kind, "--serial", serial, "--purpose", purpose]
    raise SystemExit(authority.main())

with authority.DeviceLease(kind, serial, purpose=purpose) as lease:
    ready = {"resource_lease": "held", "identity": lease.identity}
    if mode == "timeout":
        (Path(lock_root) / "entered").write_text("held", encoding="ascii")
        threading.Event().wait(60)
    elif mode == "empty":
        pass
    elif mode == "partial":
        sys.stdout.write(json.dumps(ready))
        sys.stdout.flush()
    else:
        if mode == "noisy":
            sys.stderr.write("x" * 131072)
            sys.stderr.flush()
        if mode == "malformed":
            line = b"not JSON\n"
        elif mode == "invalid-utf8":
            line = b"\xff\n"
        elif mode == "oversized":
            line = b"x" * 8192 + b"\n"
        elif mode == "mismatch":
            line = json.dumps(dict(ready, identity="jlink:999")).encode() + b"\n"
        elif mode == "extra-field":
            line = json.dumps(dict(ready, token="not-accepted")).encode() + b"\n"
        elif mode == "wrong-type":
            line = b"[]\n"
        else:
            line = json.dumps(ready).encode() + b"\n"
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        if mode != "ready-exit":
            sys.stdin.buffer.read()
            if mode == "ignore-eof":
                threading.Event().wait(60)
            if mode == "nonzero":
                print("controlled close failure", file=sys.stderr, flush=True)
                raise SystemExit(23)
"""


class ResourceLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = resource_lease.nrftest_root()
        except resource_lease.ResourceLeaseError as error:
            self.skipTest(
                f"cross-repo broker tests require a configured nrftest checkout: {error}"
            )
        if not (self.root / "host" / "nrftest" / "resource_lease.py").is_file():
            self.skipTest(
                "configured checkout does not contain the nrftest lease broker"
            )
        self.lock_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mode = "real"
        self.read_fault: str | None = None
        self.children: list[SimpleNamespace] = []
        self.readers: list[threading.Thread] = []
        self.leases: list[resource_lease.DeviceLease] = []
        popen = subprocess.Popen
        thread = threading.Thread

        def command(lease: resource_lease.DeviceLease) -> tuple[list[str], Path]:
            if lease not in self.leases:
                self.leases.append(lease)
            return [
                sys.executable,
                "-u",
                "-c",
                BROKER_SOURCE,
                str(self.lock_root),
                self.mode,
                lease._kind,
                lease._serial,
                lease._purpose,
            ], self.root

        def launch(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
            process = popen(*args, **kwargs)
            self.children.append(
                SimpleNamespace(
                    process=process,
                    stdin=process.stdin,
                    stdout=process.stdout,
                    stderr=kwargs["stderr"],
                )
            )
            if self.read_fault is not None:
                assert process.stdout is not None
                readline = process.stdout.readline
                fault = self.read_fault

                def read(*args: object) -> bytes:
                    line = readline(*args)
                    if fault == "read":
                        raise OSError("injected readiness read failure")
                    process.wait(timeout=5)
                    return line

                self.enterContext(
                    patch.object(process.stdout, "readline", side_effect=read)
                )
            return process

        def reader(*args: object, **kwargs: object) -> threading.Thread:
            result = thread(*args, **kwargs)
            self.readers.append(result)
            return result

        self.enterContext(
            patch.object(
                resource_lease.DeviceLease,
                "_command",
                autospec=True,
                side_effect=command,
            )
        )
        self.enterContext(
            patch.object(resource_lease.subprocess, "Popen", side_effect=launch)
        )
        self.enterContext(
            patch.object(resource_lease.threading, "Thread", side_effect=reader)
        )
        # Safety net only: assertions below must pass before this can reap leaks.
        self.addCleanup(self.reap_children)

    def reap_children(self) -> None:
        for child in self.children:
            process = child.process
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        for reader in self.readers:
            reader.join(timeout=5)

    def lease(
        self, kind: str = "jlink", serial: str = "123"
    ) -> resource_lease.DeviceLease:
        lease = resource_lease.DeviceLease(
            kind, serial, purpose="synthetic lifecycle test"
        )
        self.leases.append(lease)
        return lease

    def assert_clean(
        self, lease: resource_lease.DeviceLease, child: SimpleNamespace
    ) -> None:
        self.assertIsNotNone(
            child.process.poll(), "broker must be reaped, not left running"
        )
        for stream in (child.stdin, child.stdout, child.stderr):
            self.assertTrue(stream.closed)
        self.assertFalse(lease._held)
        self.assertIsNone(lease._pid)
        self.assertIsNone(lease._process)
        self.assertIsNone(lease._stderr)
        for reader in self.readers:
            reader.join(timeout=5)
            self.assertFalse(
                reader.is_alive(), "readiness thread must not survive cleanup"
            )

    def assert_reacquirable(self, kind: str = "jlink", serial: str = "123") -> None:
        self.mode = "real"
        self.read_fault = None
        lease = self.lease(kind, serial)
        with lease:
            lease.require(kind, serial)
        self.assert_clean(lease, self.children[-1])

    def test_actual_alias_conflict_and_distinct_devices_coexist(self) -> None:
        for kind, serial, alias, other in (
            ("jlink", "123", "000123", "456"),
            ("nrf-usb", "test-dongle", "TEST-DONGLE", "other-test-dongle"),
        ):
            with self.subTest(kind=kind):
                owner = self.lease(kind, serial)
                with owner:
                    owner.require(kind, alias)
                    conflict = self.lease(kind, alias)
                    with self.assertRaises(resource_lease.ResourceLeaseError) as caught:
                        conflict.acquire()
                    message = str(caught.exception)
                    self.assertIn(f"physical fixture {owner.identity} is busy", message)
                    self.assertIn("refusing synthetic lifecycle test", message)
                    self.assert_clean(conflict, self.children[-1])
                    distinct = self.lease(kind, other)
                    with distinct:
                        distinct.require(kind, other)
                        owner.require(kind, serial)
                    self.assert_clean(distinct, self.children[-1])
                    count = len(self.children)
                    with self.assertRaisesRegex(
                        resource_lease.ResourceLeaseError, "already held"
                    ):
                        owner.acquire()
                    self.assertEqual(len(self.children), count)
                self.assert_reacquirable(kind, alias)

    def test_private_stdin_eof_releases_and_same_object_can_reacquire(self) -> None:
        owner = self.lease()
        owner.acquire()
        child = self.children[-1]
        self.assertIsNotNone(child.process.stdin)
        child.stdin.close()
        self.assertEqual(child.process.wait(timeout=5), 0)
        with self.assertRaisesRegex(resource_lease.ResourceLeaseError, "not active"):
            owner.require("jlink", "123")
        # The kernel lock is free even before the wrapper clears its stale state.
        self.assert_reacquirable(serial="000123")
        owner.release()
        self.assert_clean(owner, child)
        owner.release()
        with owner:
            owner.require("jlink", "000123")
        self.assert_clean(owner, self.children[-1])

    def test_startup_protocol_failures_reap_broker_and_allow_reacquisition(
        self,
    ) -> None:
        for mode, message in (
            ("malformed", "broker failed"),
            ("invalid-utf8", "broker failed"),
            ("oversized", "bounded ready line"),
            ("partial", "bounded ready line"),
            ("empty", "bounded ready line"),
            ("mismatch", "identity mismatch"),
            ("extra-field", "identity mismatch"),
            ("wrong-type", "identity mismatch"),
        ):
            with self.subTest(mode=mode):
                self.mode = mode
                lease = self.lease()
                with self.assertRaisesRegex(resource_lease.ResourceLeaseError, message):
                    lease.acquire()
                self.assert_clean(lease, self.children[-1])
                self.assert_reacquirable()

    def test_startup_read_failure_and_exit_after_ready_are_cleaned_up(self) -> None:
        for fault, mode, message in (
            ("read", "real", "read failed: injected readiness read failure"),
            ("exit", "ready-exit", "exited during startup"),
        ):
            with self.subTest(fault=fault):
                self.read_fault = fault
                self.mode = mode
                lease = self.lease()
                with self.assertRaisesRegex(resource_lease.ResourceLeaseError, message):
                    lease.acquire()
                self.assert_clean(lease, self.children[-1])
                self.assert_reacquirable()

    def test_startup_timeout_kills_blocked_reader_and_releases_real_lock(self) -> None:
        self.mode = "timeout"
        lease = self.lease()
        with (
            patch.object(resource_lease, "STARTUP_TIMEOUT_SECONDS", 1),
            patch.object(resource_lease, "CLOSE_TIMEOUT_SECONDS", 0.2),
            self.assertRaisesRegex(
                resource_lease.ResourceLeaseError, "readiness timed out"
            ) as caught,
        ):
            lease.acquire()
        self.assertTrue((self.lock_root / "entered").is_file())
        self.assertIn("forced=True", str(caught.exception))
        self.assert_clean(lease, self.children[-1])
        self.assert_reacquirable()

    def test_spawn_failure_closes_temporary_stderr_and_can_retry(self) -> None:
        lease = self.lease()
        files = []
        temporary_file = tempfile.TemporaryFile

        def capture_stderr() -> object:
            stream = temporary_file()
            files.append(stream)
            return stream

        with (
            patch.object(
                resource_lease.tempfile, "TemporaryFile", side_effect=capture_stderr
            ),
            patch.object(
                resource_lease.subprocess, "Popen", side_effect=OSError("spawn failed")
            ),
            self.assertRaisesRegex(resource_lease.ResourceLeaseError, "spawn failed"),
        ):
            lease.acquire()
        self.assertTrue(files[0].closed)
        self.assertIsNone(lease._stderr)
        self.assertIsNone(lease._process)
        self.assertIsNone(lease._pid)
        with lease:
            lease.require("jlink", "123")
        self.assert_clean(lease, self.children[-1])

    def test_stderr_larger_than_pipe_capacity_does_not_block_readiness(self) -> None:
        self.mode = "noisy"
        lease = self.lease()
        with lease:
            child = self.children[-1]
            self.assertEqual(os.fstat(child.stderr.fileno()).st_size, 131072)
            lease.require("jlink", "123")
        self.assert_clean(lease, child)
        self.assert_reacquirable()

    def test_nonzero_exit_and_close_timeout_reap_and_release(self) -> None:
        for mode, message in (
            ("nonzero", "exit=23, forced=False: controlled close failure"),
            ("ignore-eof", "forced=True"),
        ):
            with self.subTest(mode=mode):
                self.mode = mode
                lease = self.lease()
                lease.acquire()
                with (
                    patch.object(resource_lease, "CLOSE_TIMEOUT_SECONDS", 0.2),
                    self.assertRaisesRegex(resource_lease.ResourceLeaseError, message),
                ):
                    lease.release()
                self.assert_clean(lease, self.children[-1])
                lease.release()
                self.assert_reacquirable()

    def test_stream_close_failure_still_reaps_and_closes_other_resources(self) -> None:
        for name in ("stdin", "stdout", "stderr"):
            with self.subTest(stream=name):
                lease = self.lease()
                lease.acquire()
                child = self.children[-1]
                stream = getattr(child, name)
                close = stream.close
                calls = 0

                def fail_close_once(
                    name: str = name,
                    close: Callable[[], None] = close,
                ) -> None:
                    nonlocal calls
                    calls += 1
                    # stdin fails before sending EOF; the broker still owns its lock.
                    if name == "stdin" and calls == 1:
                        raise OSError("injected stdin close failure")
                    close()
                    if calls == 1:
                        raise OSError(f"injected {name} close failure")

                with (
                    patch.object(stream, "close", side_effect=fail_close_once),
                    patch.object(resource_lease, "CLOSE_TIMEOUT_SECONDS", 0.2),
                    self.assertRaisesRegex(OSError, f"injected {name} close failure"),
                ):
                    lease.release()
                self.assert_clean(lease, child)
                self.assert_reacquirable()

    def test_wait_and_stderr_read_failures_still_reap_and_release(self) -> None:
        for operation in ("wait", "seek", "read"):
            with self.subTest(operation=operation):
                lease = self.lease()
                lease.acquire()
                child = self.children[-1]
                target = child.process if operation == "wait" else child.stderr
                original = getattr(target, operation)
                calls = 0

                def fail_once(
                    *args: object,
                    original=original,
                    operation: str = operation,
                    **kwargs: object,
                ) -> object:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError(f"injected {operation} failure")
                    return original(*args, **kwargs)

                with (
                    patch.object(target, operation, side_effect=fail_once),
                    self.assertRaisesRegex(OSError, f"injected {operation} failure"),
                ):
                    lease.release()
                self.assert_clean(lease, child)
                self.assert_reacquirable()

    def test_scope_preserves_body_exception_and_notes_cleanup_failure(self) -> None:
        self.mode = "nonzero"
        lease = self.lease()
        failure = ValueError("body failed")
        with self.assertRaises(ValueError) as caught, lease:
            raise failure
        self.assertIs(caught.exception, failure)
        self.assertTrue(
            any("controlled close failure" in note for note in failure.__notes__)
        )
        self.assert_clean(lease, self.children[-1])
        self.assert_reacquirable()

    def test_borrowed_identity_activity_and_process_must_match(self) -> None:
        owner = self.lease()
        with owner:
            child = self.children[-1]
            count = len(self.children)
            with resource_lease.owned_lease(
                "jlink", "000123", purpose="borrow", lease=owner
            ) as borrowed:
                self.assertIs(borrowed, owner)
            owner.require("jlink", "123")
            for kind, serial in (("jlink", "456"), ("nrf-usb", "123")):
                with (
                    self.subTest(kind=kind, serial=serial),
                    self.assertRaisesRegex(
                        resource_lease.ResourceLeaseError, "not active"
                    ),
                    resource_lease.owned_lease(
                        kind, serial, purpose="borrow", lease=owner
                    ),
                ):
                    self.fail("mismatched borrower entered its scope")
            with patch.object(
                resource_lease.os, "getpid", return_value=os.getpid() + 1
            ):
                with self.assertRaisesRegex(
                    resource_lease.ResourceLeaseError, "not active"
                ):
                    owner.require("jlink", "123")
                with self.assertRaisesRegex(
                    resource_lease.ResourceLeaseError, "inherited"
                ):
                    owner.release()
            self.assertEqual(len(self.children), count)
            self.assertIsNone(child.process.poll())
        self.assert_clean(owner, child)
        with (
            self.assertRaisesRegex(resource_lease.ResourceLeaseError, "not active"),
            resource_lease.owned_lease("jlink", "123", purpose="borrow", lease=owner),
        ):
            self.fail("released borrower entered its scope")
        with (
            self.assertRaisesRegex(
                resource_lease.ResourceLeaseError, "DeviceLease object"
            ),
            resource_lease.owned_lease("jlink", "123", purpose="borrow", lease="token"),
        ):
            self.fail("a string must not authorize borrowing")
        self.assert_reacquirable()

    @staticmethod
    def port(**changes: object) -> SimpleNamespace:
        fields = {
            "device": "test-port",
            "vid": 0x1366,
            "pid": 0x1061,
            "serial_number": "000123",
            "location": "synthetic-usb-location",
        }
        return SimpleNamespace(**(fields | changes))

    def test_port_identity_changes_fail_before_serial_open_and_release_owner(
        self,
    ) -> None:
        for after in (
            [self.port(serial_number="456")],
            [self.port(vid=0x2FE3, pid=0x0004)],
            [self.port(location="different-synthetic-location")],
            [self.port(vid=0, pid=0)],
            [self.port(serial_number=None)],
            [],
            [self.port(), self.port()],
        ):
            with self.subTest(after=after):
                with (
                    patch.object(
                        list_ports, "comports", side_effect=[[self.port()], after]
                    ),
                    patch.object(direct_fixture, "open_serial") as open_serial,
                    self.assertRaises(resource_lease.ResourceLeaseError),
                ):
                    direct_fixture.DirectFixture(
                        "test-port", Path("unused-profile.json")
                    )
                open_serial.assert_not_called()
                self.assert_clean(self.leases[-1], self.children[-1])
                self.assert_reacquirable()

    def test_port_revalidation_canonicalizes_alias_and_does_not_release_borrower(
        self,
    ) -> None:
        owner = self.lease()
        with owner:
            child = self.children[-1]
            with (
                patch.object(
                    list_ports,
                    "comports",
                    side_effect=[
                        [self.port()],
                        [self.port(serial_number="123")],
                    ],
                ),
                resource_lease.port_lease(
                    "test-port", purpose="borrow", lease=owner
                ) as borrowed,
            ):
                self.assertIs(borrowed, owner)
            with (
                patch.object(
                    list_ports,
                    "comports",
                    side_effect=[
                        [self.port()],
                        [self.port(location="changed")],
                    ],
                ),
                patch.object(direct_fixture, "open_serial") as open_serial,
                self.assertRaisesRegex(
                    resource_lease.ResourceLeaseError, "USB identity changed"
                ),
            ):
                direct_fixture.DirectFixture(
                    "test-port", Path("unused-profile.json"), lease=owner
                )
            open_serial.assert_not_called()
            owner.require("jlink", "123")
            self.assertIsNone(child.process.poll())
            self.assertEqual(len(self.children), 1)
        self.assert_clean(owner, child)
        self.assert_reacquirable()


class ResourceLeaseConfigurationTests(unittest.TestCase):
    def test_missing_configuration_skips_cross_repo_tests_without_starting_brokers(
        self,
    ) -> None:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ResourceLeaseTests)
        result = unittest.TestResult()
        with (
            patch.object(
                resource_lease,
                "resolve_local_value",
                return_value=SimpleNamespace(value=None),
            ),
            patch.object(resource_lease.subprocess, "Popen") as popen,
        ):
            suite.run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertGreater(result.testsRun, 0)
        self.assertEqual(len(result.skipped), result.testsRun)
        self.assertTrue(
            all("configured nrftest checkout" in reason for _, reason in result.skipped)
        )
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
