"""Device-free Appium ownership, bounded HTTP and process cleanup regressions."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import UUID

from tools import mobile_appium as appium


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError("negative wait")
        self.now += seconds


def response(body: bytes, status: str = "200 OK") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


READY = response(b'{"value":{"ready":true}}')


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = Clock()
        self.stack.enter_context(
            patch.object(appium.time, "monotonic", lambda: self.clock.now)
        )
        self.stack.enter_context(patch.object(appium.time, "sleep", self.clock.sleep))
        self.connection = self.stack.enter_context(patch("socket.create_connection"))
        self.sock = self.connection.return_value.__enter__.return_value
        self.sock.recv.side_effect = [READY, b""]

    def test_only_explicit_boolean_true_is_ready(self) -> None:
        for document in (
            {},
            {"value": {}},
            [],
            {"value": {"ready": None}},
            {"value": {"ready": 1}},
            {"value": {"ready": "true"}},
        ):
            with self.subTest(document=document):
                self.sock.recv.side_effect = [
                    response(json.dumps(document).encode()),
                    b"",
                ] * 8
                with self.assertRaises(appium.AppiumError):
                    appium.wait_for_server("http://127.0.0.1:1/nonce", 0.1)

    def test_false_polls_then_true_and_all_waits_are_clamped(self) -> None:
        self.sock.recv.side_effect = [
            response(b'{"value":{"ready":false}}'),
            b"",
            READY,
            b"",
        ]
        appium.wait_for_server("http://127.0.0.1:1/nonce", 0.3)
        self.assertEqual(self.connection.call_count, 2)
        self.assertAlmostEqual(self.clock.now, 0.25)
        self.assertLessEqual(self.connection.call_args.kwargs["timeout"], 0.05 + 1e-9)
        self.assertIn(
            b"GET /nonce/status HTTP/1.1\r\n", self.sock.sendall.call_args.args[0]
        )
        self.assertIn(b"Connection: close", self.sock.sendall.call_args.args[0])
        for call in self.sock.settimeout.call_args_list:
            self.assertGreater(call.args[0], 0)
            self.assertLessEqual(call.args[0], 0.3)

    def test_false_and_connection_failure_exhaust_only_total_budget(self) -> None:
        for failure in (None, OSError("not listening")):
            with self.subTest(failure=failure):
                self.clock.now = 0
                self.connection.side_effect = failure
                self.sock.recv.side_effect = [
                    response(b'{"value":{"ready":false}}'),
                    b"",
                ] * 4
                with self.assertRaises(appium.AppiumError):
                    appium.wait_for_server("http://127.0.0.1:1/nonce", 0.1)
                self.assertAlmostEqual(self.clock.now, 0.1)

    def test_malformed_truncated_redirect_and_oversized_http_never_succeed(
        self,
    ) -> None:
        for raw in (
            response(b"not json"),
            response(b"\xff"),
            b"garbage\r\n\r\n",
            READY[:-4],
            response(b'{"value":{"ready":true}}', "302 Found"),
            response(b" " * 65536 + b'{"value":{"ready":true}}'),
        ):
            with self.subTest(raw=raw[:50]):
                pending = bytearray(raw)

                def recv(limit: int, pending: bytearray = pending) -> bytes:
                    chunk = bytes(pending[:limit])
                    del pending[:limit]
                    return chunk

                self.sock.recv.side_effect = recv
                with self.assertRaises(appium.AppiumError):
                    appium.wait_for_server("http://127.0.0.1:1/nonce", 0.1)
                self.assertLessEqual(len(raw) - len(pending), 65536)

    def test_slow_trickle_cannot_extend_deadline(self) -> None:
        def recv(limit: int) -> bytes:
            self.clock.sleep(0.04)
            return b"H"

        self.sock.recv.side_effect = recv
        with self.assertRaises(appium.AppiumError):
            appium.wait_for_server("http://127.0.0.1:1/nonce", 0.1)
        self.assertEqual(self.sock.recv.call_count, 3)
        self.assertLessEqual(self.clock.now, 0.12 + 1e-9)
        self.assertLessEqual(self.sock.settimeout.call_args.args[0], 0.02 + 1e-9)

    def test_invalid_timeout_rejected_before_io(self) -> None:
        for timeout in (None, True, 0, -1, float("nan"), float("inf"), "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                appium.wait_for_server("http://127.0.0.1:1/nonce", timeout)
        self.connection.assert_not_called()

    def test_guard_failure_after_http_is_not_retried(self) -> None:
        for transport_failure in (None, OSError("HTTP failed")):
            with self.subTest(transport_failure=transport_failure):
                self.connection.reset_mock()
                self.connection.side_effect = transport_failure
                failure = appium.AppiumError("owner expired")
                guard = Mock(side_effect=[None, failure])
                with self.assertRaises(appium.AppiumError) as caught:
                    appium.wait_for_server(
                        "http://127.0.0.1:1/nonce", 1, require_owner=guard
                    )
                self.assertIs(caught.exception, failure)
                self.assertEqual(self.connection.call_count, 1)

    def test_guard_before_http_prevents_connection(self) -> None:
        guard = Mock(side_effect=RuntimeError("not owned"))
        with self.assertRaisesRegex(RuntimeError, "not owned"):
            appium.wait_for_server("http://127.0.0.1:1/nonce", 1, require_owner=guard)
        self.connection.assert_not_called()

    def test_ambiguous_json_is_not_ready(self) -> None:
        for body in (
            b'{"value":{"ready":false,"ready":true}}',
            b'{"value":{"ready":true},"invalid":NaN}',
        ):
            with self.subTest(body=body):
                self.sock.recv.side_effect = [response(body), b""]
                with self.assertRaises(appium.AppiumError):
                    appium.wait_for_server("http://127.0.0.1:1/nonce", 0.1)


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.spawn = self.stack.enter_context(patch.object(appium.subprocess, "Popen"))
        self.child = self.spawn.return_value
        self.child.pid = 987654
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.killpg = self.stack.enter_context(patch.object(appium.os, "killpg"))
        self.connection = self.stack.enter_context(patch("socket.create_connection"))
        self.sock = self.connection.return_value.__enter__.return_value
        self.sock.recv.side_effect = [READY, b""]
        self.probe = self.stack.enter_context(patch("socket.socket"))
        self.server = self.make_server()

    def make_server(self, **overrides) -> appium.AppiumServer:
        options = {
            "node": "unused-node",
            "entry": Path("unused-appium.js"),
            "port": 4723,
            "log": self.directory / "appium.log",
            "timeout": 0.01,
        }
        options.update(overrides)
        return appium.AppiumServer(**options)

    def test_nonce_probe_exclusive_log_and_one_shot(self) -> None:
        self.server.start()
        url = urlsplit(self.server.base_url)
        self.assertTrue(url.path.startswith("/blehub-"))
        UUID(url.path.removeprefix("/blehub-"))
        args = self.spawn.call_args.args[0]
        self.assertEqual(args[args.index("--base-path") + 1], url.path)
        self.assertEqual(args[args.index("--log-level") + 1], "info")
        self.assertNotIn("--log-timestamp", args)
        self.assertNotIn("--log-no-colors", args)
        self.assertTrue(self.spawn.call_args.kwargs["start_new_session"])
        self.probe.return_value.__enter__.return_value.bind.assert_called_once_with(
            ("127.0.0.1", 4723)
        )
        self.server.require_running()
        with self.assertRaises(appium.AppiumError):
            self.server.start()
        self.spawn.assert_called_once()
        self.server.stop()
        with self.assertRaises(appium.AppiumError):
            self.server.require_running()
        with self.assertRaises(appium.AppiumError):
            self.server.start()
        self.server.stop()
        self.killpg.assert_called_once_with(self.child.pid, signal.SIGTERM)

    def test_explicit_debug_logging_keeps_owned_endpoint_and_timestamped_output(
        self,
    ) -> None:
        server = self.make_server(debug_logging=True)
        server.start()
        args = self.spawn.call_args.args[0]
        self.assertEqual(args[args.index("--log-level") + 1], "debug")
        self.assertEqual(
            args[args.index("--base-path") + 1], urlsplit(server.base_url).path
        )
        self.assertIn("--log-timestamp", args)
        self.assertIn("--log-no-colors", args)
        self.assertTrue(self.spawn.call_args.kwargs["start_new_session"])
        server.stop()
        self.spawn.assert_called_once()

    def test_debug_logging_requires_boolean_before_io(self) -> None:
        for value in (None, 0, 1, "debug", []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make_server(debug_logging=value)
        self.spawn.assert_not_called()
        self.connection.assert_not_called()
        self.probe.assert_not_called()

    def test_base_url_is_read_only_and_stays_bound_to_spawn_nonce(self) -> None:
        original = self.server.base_url
        with self.assertRaises(AttributeError):
            self.server.base_url = "http://127.0.0.1:4723"
        self.server.start()
        with self.assertRaises(AttributeError):
            self.server.base_url = "http://127.0.0.1:4723/foreign"
        self.assertEqual(self.server.base_url, original)
        command = self.spawn.call_args.args[0]
        self.assertEqual(
            command[command.index("--base-path") + 1], urlsplit(original).path
        )
        self.assertIn(
            f"GET {urlsplit(original).path}/status HTTP/1.1".encode(),
            self.sock.sendall.call_args.args[0],
        )

    def test_start_deadline_clamps_http_to_earlier_budget_without_reset(self) -> None:
        wait_for_server = appium.wait_for_server
        clock = Clock()
        expected_deadline = 101
        for timeout, caller_deadline in ((10, 101), (1, 110.0)):
            with self.subTest(timeout=timeout, caller_deadline=caller_deadline):
                clock.now = 100
                server = self.make_server(
                    timeout=timeout, log=self.directory / f"deadline-{timeout}.log"
                )

                def spawn(*args, **kwargs):
                    clock.sleep(0.25)
                    return self.child

                def handoff(*args, **kwargs) -> None:
                    # Time between computing the relative remainder and entering
                    # the readiness helper must not extend the absolute budget.
                    clock.sleep(0.125)
                    wait_for_server(*args, **kwargs)

                def connect(address, *, timeout: float):
                    self.assertAlmostEqual(timeout, 0.625)
                    self.assertLessEqual(clock.now + timeout, expected_deadline)
                    clock.sleep(timeout)
                    raise TimeoutError("bounded HTTP wait")

                self.spawn.side_effect = spawn
                self.connection.side_effect = connect
                with (
                    patch.object(appium.time, "monotonic", lambda: clock.now),
                    patch.object(appium.time, "sleep", clock.sleep),
                    patch.object(appium, "wait_for_server", side_effect=handoff),
                    self.assertRaises(appium.AppiumError),
                ):
                    server.start(deadline=caller_deadline, require_owner=Mock())
                self.assertAlmostEqual(clock.now, expected_deadline)
                self.killpg.assert_not_called()
                with self.assertRaises(appium.AppiumError):
                    server.require_running()

    def test_start_rejects_invalid_absolute_deadline_before_side_effects(self) -> None:
        for deadline in (
            True,
            False,
            "101",
            float("nan"),
            float("inf"),
            -float("inf"),
            object(),
        ):
            with self.subTest(deadline=deadline):
                server = self.make_server()
                owner = Mock()
                with self.assertRaises(ValueError):
                    server.start(deadline=deadline, require_owner=owner)
                owner.assert_not_called()
        self.probe.assert_not_called()
        self.spawn.assert_not_called()
        self.connection.assert_not_called()
        self.assertFalse((self.directory / "appium.log").exists())

    def test_start_expired_absolute_deadline_does_not_spawn_or_poll(self) -> None:
        for deadline in (-1, 0, 100.0):
            with self.subTest(deadline=deadline):
                server = self.make_server()
                with (
                    patch.object(appium.time, "monotonic", return_value=100.0),
                    self.assertRaises(appium.AppiumError),
                ):
                    server.start(deadline=deadline)
        self.probe.assert_not_called()
        self.spawn.assert_not_called()
        self.connection.assert_not_called()
        self.assertFalse((self.directory / "appium.log").exists())

    def test_start_accepts_ready_child_with_absolute_deadline(self) -> None:
        with patch.object(appium.time, "monotonic", return_value=100.0):
            self.server.start(deadline=100.005, require_owner=Mock())
        self.assertAlmostEqual(self.connection.call_args.kwargs["timeout"], 0.005)
        self.server.require_running()

    def test_existing_log_is_never_overwritten_even_after_failed_start(self) -> None:
        log = self.directory / "appium.log"
        log.write_text("prior evidence", encoding="utf-8")
        with self.assertRaises((appium.AppiumError, FileExistsError)):
            self.server.start()
        self.assertEqual(log.read_text(encoding="utf-8"), "prior evidence")
        self.spawn.assert_not_called()
        with self.assertRaises(appium.AppiumError):
            self.server.start()

    def test_occupied_port_refuses_spawn(self) -> None:
        self.probe.return_value.__enter__.return_value.bind.side_effect = OSError(
            "in use"
        )
        with self.assertRaises(appium.AppiumError):
            self.server.start()
        self.spawn.assert_not_called()
        self.connection.assert_not_called()

    def test_failed_spawn_is_one_shot(self) -> None:
        self.spawn.side_effect = OSError("cannot spawn")
        with self.assertRaises(OSError):
            self.server.start()
        with self.assertRaises(appium.AppiumError):
            self.server.start()
        self.server.stop()
        self.killpg.assert_not_called()

    def test_owner_checked_before_spawn_and_each_http_side(self) -> None:
        owner = Mock()
        self.server.start(require_owner=owner)
        self.assertGreaterEqual(owner.call_count, 3)
        self.server.require_running()
        count = owner.call_count
        self.server.require_running()
        self.assertEqual(owner.call_count, count)
        self.connection.assert_called_once()

    def test_expired_owner_before_spawn_performs_no_io(self) -> None:
        owner = Mock(side_effect=RuntimeError("lease expired"))
        with self.assertRaisesRegex(RuntimeError, "lease expired"):
            self.server.start(require_owner=owner)
        self.spawn.assert_not_called()
        self.connection.assert_not_called()

    def test_child_death_before_http_and_after_response_never_ready(self) -> None:
        for before_http in (True, False):
            with self.subTest(before_http=before_http):
                server = self.make_server(log=self.directory / str(before_http))
                self.child.poll.return_value = 7 if before_http else None
                self.sock.recv.side_effect = [READY, b""]
                self.connection.reset_mock()

                def send(data: bytes) -> None:
                    self.child.poll.return_value = 7

                self.sock.sendall.side_effect = send
                with self.assertRaises(appium.AppiumError):
                    server.start()
                if before_http:
                    self.connection.assert_not_called()
                self.killpg.assert_not_called()
                with self.assertRaises(appium.AppiumError):
                    server.require_running()

    def test_owner_loss_after_response_requires_explicit_caller_stop(self) -> None:
        expired = False

        def owner() -> None:
            if expired:
                raise RuntimeError("lease expired")

        def send(data: bytes) -> None:
            nonlocal expired
            expired = True

        self.sock.sendall.side_effect = send
        with self.assertRaisesRegex(RuntimeError, "lease expired"):
            self.server.start(require_owner=owner)
        self.killpg.assert_not_called()
        with self.assertRaises(appium.AppiumError):
            self.server.require_running()
        self.server.stop(timeout=0.1)
        self.killpg.assert_called_once_with(self.child.pid, signal.SIGTERM)

    def test_require_running_rejects_replacement_without_touching_http(self) -> None:
        with self.assertRaises(appium.AppiumError):
            self.server.require_running()
        self.server.start()
        self.server._process = Mock(pid=111111)
        with self.assertRaises(appium.AppiumError):
            self.server.require_running()
        with self.assertRaises(appium.AppiumError):
            self.server.stop(timeout=0.1)
        self.assertTrue(
            all(call.args[0] == self.child.pid for call in self.killpg.call_args_list)
        )
        self.connection.assert_called_once()

    def test_term_timeout_kills_reaps_with_one_budget_and_failure_is_sticky(
        self,
    ) -> None:
        self.server.start()
        clock = Clock()
        waits = []

        def wait(*, timeout: float) -> int:
            waits.append(timeout)
            if len(waits) == 1:
                clock.sleep(timeout)
                raise subprocess.TimeoutExpired("owned child", timeout)
            self.child.poll.return_value = -signal.SIGKILL
            return -signal.SIGKILL

        self.child.wait.side_effect = wait
        with patch.object(appium.time, "monotonic", lambda: clock.now):
            with self.assertRaisesRegex(
                appium.AppiumError, "(?i)timeout|timed out"
            ) as first:
                self.server.stop(timeout=0.4)
            with self.assertRaises(appium.AppiumError) as second:
                self.server.stop(timeout=2)
        self.assertEqual(str(first.exception), str(second.exception))
        self.assertEqual(
            [c.args for c in self.killpg.call_args_list],
            [(self.child.pid, signal.SIGTERM), (self.child.pid, signal.SIGKILL)],
        )
        self.assertEqual(len(waits), 2)
        self.assertLessEqual(sum(waits), 0.4 + 1e-9)
        self.assertGreater(waits[1], 0)

    def test_signal_errors_and_reap_failure_are_all_preserved(self) -> None:
        self.server.start()
        self.killpg.side_effect = [
            PermissionError("TERM denied"),
            PermissionError("KILL denied"),
        ]
        self.child.wait.side_effect = subprocess.TimeoutExpired("reap", 0.01)
        with self.assertRaises(appium.AppiumError) as caught:
            self.server.stop(timeout=0.02)
        for detail in ("TERM denied", "KILL denied", "reap"):
            self.assertIn(detail, str(caught.exception))
        self.assertEqual(self.child.wait.call_count, 2)
        with self.assertRaises(appium.AppiumError):
            self.server.stop()
        self.assertEqual(self.killpg.call_count, 2)

    def test_invalid_stop_budget_does_not_consume_cleanup(self) -> None:
        self.server.start()
        for timeout in (True, 0, -1, float("nan"), float("inf"), None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.server.stop(timeout=timeout)
        self.killpg.assert_not_called()
        self.server.stop(timeout=0.1)

    def test_poll_cleanup_error_cannot_become_success_on_repeat(self) -> None:
        self.server.start()
        self.child.poll.side_effect = OSError("waitpid failed")
        with self.assertRaisesRegex(appium.AppiumError, "waitpid failed"):
            self.server.stop(timeout=0.1)
        self.child.poll.side_effect = None
        with self.assertRaisesRegex(appium.AppiumError, "waitpid failed"):
            self.server.stop(timeout=0.1)
        self.killpg.assert_not_called()

    def test_interrupted_cleanup_cannot_become_success_on_repeat(self) -> None:
        self.server.start()
        self.child.wait.side_effect = KeyboardInterrupt("cleanup interrupted")
        with self.assertRaises(KeyboardInterrupt):
            self.server.stop(timeout=0.1)
        with self.assertRaisesRegex(appium.AppiumError, "cleanup interrupted"):
            self.server.stop(timeout=0.1)
        self.killpg.assert_called_once_with(self.child.pid, signal.SIGTERM)

    def test_already_exited_child_never_signals_a_reusable_group_id(self) -> None:
        self.server.start()
        self.child.poll.return_value = 7
        with self.assertRaisesRegex(appium.AppiumError, "already exited"):
            self.server.stop(timeout=0.1)
        with self.assertRaises(appium.AppiumError):
            self.server.stop(timeout=0.1)
        self.killpg.assert_not_called()

    def test_stop_before_start_permanently_closes_owner(self) -> None:
        self.server.stop()
        with self.assertRaises(appium.AppiumError):
            self.server.start()
        self.spawn.assert_not_called()


class LocalBoundaryTests(unittest.TestCase):
    def test_real_slow_http_headers_obey_total_deadline(self) -> None:
        script = """
import signal, socket, sys, time
signal.alarm(3)
with socket.socket(fileno=int(sys.argv[1])) as listener:
    connection, _ = listener.accept()
    with connection:
        connection.recv(4096)
        for _ in range(200):
            connection.sendall(b'H')
            time.sleep(0.01)
"""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(listener.fileno())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(listener.fileno(),),
                start_new_session=True,
            )
            try:
                started = time.monotonic()
                with self.assertRaises(appium.AppiumError):
                    appium.wait_for_server(f"http://127.0.0.1:{port}/nonce", 0.15)
                self.assertLess(time.monotonic() - started, 0.8)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)

    def test_live_loopback_listener_is_refused_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            server = appium.AppiumServer(
                node="unused",
                entry=Path("unused"),
                port=listener.getsockname()[1],
                log=Path(directory) / "appium.log",
                timeout=0.2,
            )
            with patch.object(appium.subprocess, "Popen") as spawn:
                with self.assertRaises(appium.AppiumError):
                    server.start()
                spawn.assert_not_called()

    def test_python_child_nonce_http_and_reap(self) -> None:
        # No Appium/WDA/SDK: this bounded child only answers local /status.
        script = """
import argparse, json, signal, socket, time
p = argparse.ArgumentParser()
p.add_argument('--port', type=int)
p.add_argument('--base-path', default='')
a, _ = p.parse_known_args()
signal.alarm(5)
with socket.socket() as listener:
    listener.bind(('127.0.0.1', a.port))
    listener.listen(1)
    connection, _ = listener.accept()
    with connection:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        data = connection.recv(4096)
        expected = ('GET ' + a.base_path + '/status HTTP/1.1').encode()
        body = json.dumps({'value': {'ready': data.startswith(expected)}}).encode()
        connection.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: ' + str(len(body)).encode() + b'\\r\\nConnection: close\\r\\n\\r\\n' + body)
    time.sleep(4)
"""
        with tempfile.TemporaryDirectory() as directory, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            entry = Path(directory) / "server.py"
            entry.write_text(script, encoding="utf-8")
            server = appium.AppiumServer(
                node=sys.executable,
                entry=entry,
                port=port,
                log=Path(directory) / "appium.log",
                timeout=2,
            )
            try:
                server.start()
                server.require_running()
                started = time.monotonic()
                with self.assertRaises(appium.AppiumError):
                    server.stop(timeout=0.3)
                self.assertLess(time.monotonic() - started, 1)
                self.assertIsNotNone(server._process.returncode)
            finally:
                child = server._process
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
