"""Offline refresh mechanics: real local children, no Pods/network/device access."""

from __future__ import annotations

import builtins
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import Mock, call, patch

from tools.react_native import ios_pod_refresh as refresh


@unittest.skipUnless(os.name == "posix", "POSIX process group contract")
class ProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_snapshot(
        self,
        script: str,
        *,
        timeout: float = 5,
        environment: Mapping[str, str] | None = None,
        deadline: float | None = None,
    ) -> str:
        return refresh.run_snapshot(
            [sys.executable, "-c", script],
            cwd=self.root,
            timeout=timeout,
            environment=environment,
            deadline=deadline,
        )

    def assert_retired(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.025)
        self.fail("owned local descendant survived retirement")

    def tree(self, mode: str, *, close_pipes: bool = False) -> str:
        """One short-lived local grandchild with a finite fail-safe lifetime."""
        child = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + ("os.close(1); os.close(2)\n" if close_pipes else "")
            + f"open({str(self.root / 'grandchild')!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(10)\n"
        )
        actions = {
            "timeout": "time.sleep(10)",
            "exit": "raise SystemExit(0)",
            "nonzero": "raise SystemExit(7)",
            "signal": "os.kill(os.getpid(), signal.SIGKILL)",
            "overflow": "os.write(2, b'x' * (17 * 1024 * 1024)); time.sleep(10)",
            "output": "print('ready', flush=True); time.sleep(10)",
        }
        return (
            "import os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            f"marker = Path({str(self.root / 'grandchild')!r})\n"
            "end = time.monotonic() + 3\n"
            "while not marker.exists() and time.monotonic() < end: time.sleep(.005)\n"
            "assert marker.exists()\n" + actions[mode]
        )

    def descendant_pid(self) -> int:
        return int((self.root / "grandchild").read_text())

    def test_snapshot_stdout_stderr_stdin_cwd_and_session(self) -> None:
        script = (
            "import json, os, sys\n"
            "assert sys.stdin.read() == ''\n"
            "assert os.getpid() == os.getpgrp() == os.getsid(0)\n"
            "assert os.environ['REFRESH_TEST'] == 'local'\n"
            "sys.stderr.write('diagnostic, not JSON')\n"
            "print(json.dumps({'cwd': os.getcwd()}))\n"
        )
        environment = {**os.environ, "REFRESH_TEST": "local"}
        result = self.run_snapshot(script, environment=environment)
        self.assertEqual(json.loads(result), {"cwd": str(self.root.resolve())})

    def test_exact_combined_snapshot_limit_and_stderr_not_in_result(self) -> None:
        result = self.run_snapshot(
            "import os; os.write(1, b'x'); "
            f"os.write(2, b'e' * ({refresh.MAX_SNAPSHOT_BYTES} - 1))"
        )
        self.assertEqual(result, "x")
        for fd in (1, 2):
            with (
                self.subTest(fd=fd),
                self.assertRaisesRegex(refresh.PodRefreshError, "output limit"),
            ):
                self.run_snapshot(
                    f"import os; os.write({fd}, b'x' * ({refresh.MAX_SNAPSHOT_BYTES} + 1))"
                )

    def test_both_full_pipes_are_drained_without_deadlock(self) -> None:
        result = self.run_snapshot(
            "import os\n"
            "for i in range(128):\n"
            " os.write(1, b'o' * 8192)\n"
            " os.write(2, b'e' * 8192)\n"
        )
        self.assertEqual(result, "o" * (128 * 8192))

    def test_nonzero_and_invalid_utf8_do_not_expose_output_or_argv(self) -> None:
        for script in (
            "import sys; print('secret-token'); sys.exit(4)",
            "import os; os.write(1, b'secret-token\\xff')",
        ):
            with (
                self.subTest(script=script),
                self.assertRaises(refresh.PodRefreshError) as caught,
            ):
                self.run_snapshot(script)
            self.assertNotIn("secret-token", str(caught.exception))

    def test_deadline_and_timeout_are_monotonic_and_retire_owned_tree(self) -> None:
        for absolute in (False, True):
            (self.root / "grandchild").unlink(missing_ok=True)
            deadline = time.monotonic() + 0.6 if absolute else None
            started = time.monotonic()
            with (
                self.subTest(absolute=absolute),
                self.assertRaisesRegex(refresh.PodRefreshError, "deadline"),
            ):
                self.run_snapshot(
                    self.tree("timeout"),
                    timeout=5 if absolute else 0.6,
                    deadline=deadline,
                )
            self.assertLess(time.monotonic() - started, 6)
            self.assert_retired(self.descendant_pid())

    def test_exited_leader_with_lingering_descendant_with_or_without_pipes(
        self,
    ) -> None:
        for close_pipes in (False, True):
            (self.root / "grandchild").unlink(missing_ok=True)
            with self.subTest(close_pipes=close_pipes):
                self.assertEqual(
                    self.run_snapshot(self.tree("exit", close_pipes=close_pipes)), ""
                )
                self.assert_retired(self.descendant_pid())

    def test_abnormal_completion_and_output_overflow_retire_tree(self) -> None:
        for mode in ("nonzero", "signal", "overflow"):
            (self.root / "grandchild").unlink(missing_ok=True)
            with self.subTest(mode=mode), self.assertRaises(refresh.PodRefreshError):
                self.run_snapshot(self.tree(mode))
            self.assert_retired(self.descendant_pid())

    def test_foreign_sibling_is_not_signalled(self) -> None:
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as sibling:
            try:
                with self.assertRaises(refresh.PodRefreshError):
                    self.run_snapshot(self.tree("timeout"), timeout=0.6)
                self.assertIsNone(sibling.poll())
                self.assert_retired(self.descendant_pid())
            finally:
                sibling.kill()
                sibling.wait(timeout=3)

    def test_logged_streams_private_permissions_no_capture_or_command_echo(
        self,
    ) -> None:
        log = self.root / "install.log"
        refresh.run_logged(
            [sys.executable, "-c", "import os; os.write(1,b'out'); os.write(2,b'err')"],
            cwd=self.root,
            timeout=5,
            log_path=log,
        )
        self.assertIn(log.read_bytes(), (b"outerr", b"errout"))
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertLess(log.stat().st_size, 10)
        for destination in (log, self.root / "linked.log"):
            if destination != log:
                destination.symlink_to(log)
            with self.assertRaises(refresh.PodRefreshError):
                refresh.run_logged(
                    ["must-not-run"], cwd=self.root, timeout=1, log_path=destination
                )
        self.assertIn(log.read_bytes(), (b"outerr", b"errout"))

    def test_log_limit_failure_retires_tree_and_preserves_partial_log(self) -> None:
        log = self.root / "install.log"
        with (
            patch.object(refresh, "MAX_LOG_BYTES", 2),
            self.assertRaisesRegex(refresh.PodRefreshError, "output limit"),
        ):
            refresh.run_logged(
                [sys.executable, "-c", self.tree("output")],
                cwd=self.root,
                timeout=5,
                log_path=log,
            )
        self.assert_retired(self.descendant_pid())
        self.assertLessEqual(log.stat().st_size, 2)

    def test_output_io_failure_and_interrupt_retire_owned_tree(self) -> None:
        for error in (OSError("private I/O detail"), KeyboardInterrupt()):
            (self.root / "grandchild").unlink(missing_ok=True)
            sink = Mock()
            sink.write.side_effect = error
            expected = (
                refresh.PodRefreshError
                if isinstance(error, OSError)
                else KeyboardInterrupt
            )
            with self.subTest(error=type(error).__name__), self.assertRaises(expected):
                refresh._run(
                    [sys.executable, "-c", self.tree("output")],
                    cwd=self.root,
                    end=time.monotonic() + 5,
                    environment=None,
                    log=sink,
                )
            self.assert_retired(self.descendant_pid())

    def test_spawn_failure_is_sanitized_and_never_signals(self) -> None:
        with (
            patch.object(refresh.os, "killpg") as kill,
            self.assertRaisesRegex(refresh.PodRefreshError, "I/O failed") as caught,
        ):
            refresh.run_snapshot(
                ["/does-not-exist/private-token"], cwd=self.root, timeout=1
            )
        self.assertNotIn("private-token", str(caught.exception))
        kill.assert_not_called()

    def test_invalid_and_expired_budgets_never_submit(self) -> None:
        with patch.object(refresh.subprocess, "Popen") as spawn:
            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    self.run_snapshot("pass", timeout=timeout)
            with self.assertRaises(ValueError):
                self.run_snapshot("pass", deadline=float("nan"))
            with self.assertRaises(refresh.PodRefreshError):
                self.run_snapshot("pass", deadline=time.monotonic() - 1)
            spawn.assert_not_called()

    def test_cleanup_allowance_is_separate_from_execution_deadline(self) -> None:
        self.assertEqual(self.run_snapshot("print('done')", timeout=0.2), "done\n")

    def test_partial_log_survives_nonzero_exit(self) -> None:
        log = self.root / "failure.log"
        with self.assertRaises(refresh.PodRefreshError):
            refresh.run_logged(
                [sys.executable, "-c", "import sys; print('partial'); sys.exit(1)"],
                cwd=self.root,
                timeout=5,
                log_path=log,
            )
        self.assertEqual(log.read_bytes(), b"partial\n")

    def test_waitid_exit_observation_does_not_reap(self) -> None:
        watch = refresh._ExitWatch.__new__(refresh._ExitWatch)
        watch.queue = None
        waitid = Mock(return_value=None)
        with patch.multiple(
            refresh.os,
            create=True,
            waitid=waitid,
            P_PID=1,
            WEXITED=2,
            WNOHANG=4,
            WNOWAIT=8,
        ):
            self.assertFalse(watch.exited(12345))
            waitid.return_value = object()
            self.assertTrue(watch.exited(12345))
        self.assertEqual(waitid.call_args_list, [call(1, 12345, 14)] * 2)
        watch.close()

    def test_watch_setup_failure_retires_started_process(self) -> None:
        with (
            patch.object(refresh._ExitWatch, "register", side_effect=OSError()),
            self.assertRaisesRegex(refresh.PodRefreshError, "I/O failed"),
        ):
            self.run_snapshot("import time; time.sleep(10)")

    def test_retirement_signals_entire_unreaped_group_then_waits(self) -> None:
        process = Mock(pid=12345)
        events = Mock()
        with (
            patch.object(
                refresh.os, "killpg", side_effect=[None, None, ProcessLookupError()]
            ) as kill,
            patch.object(refresh.time, "sleep") as sleep,
        ):
            events.attach_mock(kill, "killpg")
            events.attach_mock(sleep, "sleep")
            events.attach_mock(process.wait, "wait")
            refresh._retire(process)
        self.assertEqual(
            events.mock_calls[:4],
            [
                call.killpg(12345, signal.SIGTERM),
                call.sleep(refresh.TERM_GRACE_SECONDS),
                call.killpg(12345, signal.SIGKILL),
                call.wait(timeout=refresh.KILL_WAIT_SECONDS),
            ],
        )
        process.poll.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_primary_timeout_and_retirement_failure_are_both_inspectable(self) -> None:
        watch = Mock()
        watch.exited.return_value = False

        def failing_retire(process: subprocess.Popen[bytes]) -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            raise refresh.PodRefreshError("retirement failed")

        with (
            patch.object(refresh, "_ExitWatch", return_value=watch),
            patch.object(refresh, "_retire", side_effect=failing_retire),
            self.assertRaises(builtins.ExceptionGroup) as caught,
        ):
            self.run_snapshot("import time; time.sleep(10)", timeout=0.1)
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertRegex(str(caught.exception.exceptions[0]), "deadline")
        self.assertRegex(str(caught.exception.exceptions[1]), "retirement failed")
        self.assertTrue(
            all(
                isinstance(item, refresh.PodRefreshError)
                for item in caught.exception.exceptions
            )
        )
        watch.close.assert_called_once()

    def test_retirement_and_both_stream_close_failures_are_independent(self) -> None:
        process = Mock(pid=12345)
        process.stdout.close.side_effect = OSError("stdout-secret")
        process.stderr.close.side_effect = OSError("stderr-secret")
        with patch.object(
            refresh, "_retire", side_effect=refresh.PodRefreshError("retirement failed")
        ):
            failures = refresh._close_process(process, retire=True)
        self.assertEqual(len(failures), 3)
        self.assertEqual(
            [str(item) for item in failures],
            ["retirement failed", "refresh cleanup failed", "refresh cleanup failed"],
        )
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        self.assertNotIn("secret", repr(failures))

    def test_keyboard_interrupt_and_cleanup_failure_form_base_exception_group(
        self,
    ) -> None:
        watch = Mock()
        watch.exited.side_effect = KeyboardInterrupt()

        def failing_retire(process: subprocess.Popen[bytes]) -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            raise refresh.PodRefreshError("retirement failed")

        with (
            patch.object(refresh, "_ExitWatch", return_value=watch),
            patch.object(refresh, "_retire", side_effect=failing_retire),
            self.assertRaises(builtins.BaseExceptionGroup) as caught,
        ):
            self.run_snapshot("import time; time.sleep(10)", timeout=5)
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertIsInstance(caught.exception.exceptions[0], KeyboardInterrupt)
        self.assertEqual(str(caught.exception.exceptions[1]), "retirement failed")
        watch.close.assert_called_once()

    def test_exit_watch_close_failure_is_reported_without_masking_success(self) -> None:
        watch = Mock()
        watch.exited.side_effect = [False, True]
        watch.close.side_effect = OSError("watch-secret")

        def wait_for_exit(process: subprocess.Popen[bytes]) -> None:
            process.wait(timeout=2)

        with (
            patch.object(refresh, "_ExitWatch", return_value=watch),
            patch.object(refresh, "_retire", side_effect=wait_for_exit),
            self.assertRaisesRegex(refresh.PodRefreshError, "cleanup failed"),
        ):
            self.run_snapshot("print('done')")
        watch.close.assert_called_once()

    def test_kill_wait_is_bounded_and_failed_retirement_is_not_retried(self) -> None:
        process = Mock(pid=12345)
        process.wait.side_effect = subprocess.TimeoutExpired("not logged", 2)
        with (
            patch.object(refresh.os, "killpg") as kill,
            patch.object(refresh.time, "sleep"),
            self.assertRaisesRegex(refresh.PodRefreshError, "retirement timed out"),
        ):
            refresh._retire(process)
        self.assertEqual(
            kill.call_args_list,
            [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)],
        )
        process.wait.assert_called_once_with(timeout=refresh.KILL_WAIT_SECONDS)
        with patch.object(refresh, "_retire") as retire:
            refresh._close_process(process, retire=False)
        retire.assert_not_called()
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_zombie_only_signalling_errors_require_confirmed_group_disappearance(
        self,
    ) -> None:
        process = Mock(pid=12345)
        with (
            patch.object(
                refresh.os,
                "killpg",
                side_effect=[
                    PermissionError(),
                    PermissionError(),
                    PermissionError(),
                    ProcessLookupError(),
                ],
            ) as kill,
            patch.object(refresh.time, "sleep"),
        ):
            refresh._retire(process)
        self.assertEqual(kill.call_args_list[-2:], [call(12345, 0)] * 2)

    def test_term_failure_still_kills_and_cleanup_failure_is_not_success(self) -> None:
        process = Mock(pid=12345)
        with (
            patch.object(refresh.os, "killpg", side_effect=PermissionError()) as kill,
            patch.object(refresh.time, "sleep"),
            patch.object(refresh.time, "monotonic", side_effect=[0, 3]),
            self.assertRaisesRegex(refresh.PodRefreshError, "retirement"),
        ):
            refresh._retire(process)
        self.assertIn(call(12345, signal.SIGKILL), kill.call_args_list)
        process.wait.assert_called_once()


class CurlPolicyTests(unittest.TestCase):
    def policy(self, arguments: list[str], maximum: str) -> list[str]:
        command = refresh._curl_arguments(arguments)
        self.assertEqual(command[0], "-q")
        for option, value in (
            ("--connect-timeout", "6"),
            ("--max-time", maximum),
            ("--retry", "0"),
            ("--max-redirs", "4"),
            ("--proto", "=https"),
            ("--proto-redir", "=https"),
        ):
            self.assertEqual(command.count(option), 1)
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertEqual(command[-2:], ["--", "https://example.invalid/artifact"])
        self.assertIn("--globoff", command)
        return command

    def test_real_rn_argument_shapes(self) -> None:
        url = "https://example.invalid/artifact"
        command = self.policy(
            ["-o", "/dev/null", "--silent", "-Iw", "%{http_code}", "-L", url], "12"
        )
        self.assertIn("--head", command)
        self.policy(["-sL", url], "180")
        self.policy(
            ["-A", "react-native-0.87.1", url, "-Lo", "local file.download"], "180"
        )
        self.policy(["-Ls", "-o", "/dev/null", "-w", "%{url_effective}", url], "180")
        self.policy(["--head", "--location", url], "12")

    def test_overrides_abbreviations_short_clusters_and_injection_are_rejected(
        self,
    ) -> None:
        attacks = [
            ["--connect-timeout", "99"],
            ["--max-time=0"],
            ["-m0"],
            ["-Lm0"],
            ["--retry", "5"],
            ["--retry-all-errors"],
            ["--max-redirs", "-1"],
            ["--proto", "=all"],
            ["--proto-redir", "=http"],
            ["--next"],
            ["-:"],
            ["--"],
            ["-K", "private-config"],
            ["-sKprivate-config"],
            ["--config=private"],
            ["--conf", "private"],
            ["--no-disable"],
            ["--no-head"],
            ["--request", "HEAD"],
            ["-XGET"],
            ["--parallel"],
            ["--expand-url", "https://elsewhere.invalid"],
            ["--url", "@private"],
            ["--trace", "private"],
            ["-v"],
            ["--insecure"],
            ["-sk"],
            ["--netrc"],
            ["--user", "secret"],
            ["-w", "@private"],
            ["-w", "%output{private}"],
            ["-o"],
            ["-A", "bad\nvalue"],
            ["-w", "%{http_code}", "-w", "%{url_effective}"],
        ]
        for attack in attacks:
            with (
                self.subTest(attack=attack),
                self.assertRaises(refresh.PodRefreshError),
            ):
                refresh._curl_arguments(["https://example.invalid/artifact", *attack])

    def test_only_single_explicit_https_credential_free_url(self) -> None:
        for arguments in (
            [],
            ["http://example.invalid"],
            ["file:///private"],
            ["example.invalid"],
            ["https://user:secret@example.invalid"],
            ["https:///missing-host"],
            ["https://example.invalid/\nsecret"],
            ["https://one.invalid", "https://two.invalid"],
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(refresh.PodRefreshError),
            ):
                refresh._curl_arguments(arguments)

    def test_exec_uses_fixed_executable_and_removes_implicit_secret_logs(self) -> None:
        with (
            patch.dict(os.environ, {"SSLKEYLOGFILE": "secret", "QLOGDIR": "private"}),
            patch.object(refresh.os, "execve") as execute,
        ):
            refresh._exec_curl("/fixed/curl", ["https://example.invalid"])
        executable, arguments, environment = execute.call_args.args
        self.assertEqual(executable, "/fixed/curl")
        self.assertEqual(arguments[:2], ["/fixed/curl", "-q"])
        self.assertNotIn("SSLKEYLOGFILE", environment)
        self.assertNotIn("QLOGDIR", environment)

    def test_wrapper_rejection_does_not_log_arguments(self) -> None:
        with (
            patch.object(refresh.sys, "stderr") as stderr,
            patch.object(refresh.os, "execve") as execute,
            self.assertRaises(SystemExit) as caught,
        ):
            refresh._exec_curl("/fixed/curl", ["--trace", "private-secret"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("private-secret", str(stderr.write.call_args))
        execute.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "executable PATH wrapper")
    def test_scoped_path_fixed_resolution_real_wrapper_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "curl"
            actual.write_text(
                f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            actual.chmod(0o700)
            environment = {**os.environ, "PATH": directory}
            before = dict(environment)
            global_before = dict(os.environ)
            with refresh.curl_environment(environment) as scoped:
                shim = Path(scoped["PATH"].split(os.pathsep)[0]) / "curl"
                self.assertTrue(shim.is_file())
                self.assertNotEqual(shim, actual)
                self.assertEqual(dict(os.environ), global_before)
                command = json.loads(
                    refresh.run_snapshot(
                        ["curl", "-sIL", "https://example.invalid/artifact"],
                        cwd=root,
                        environment=scoped,
                        timeout=5,
                    )
                )
                self.assertEqual(
                    command,
                    self.policy(["-sIL", "https://example.invalid/artifact"], "12"),
                )
                # An absolute-path downloader is not intercepted.
                self.assertEqual(
                    json.loads(
                        refresh.run_snapshot(
                            [str(actual), "untouched"],
                            cwd=root,
                            environment=scoped,
                            timeout=5,
                        )
                    ),
                    ["untouched"],
                )
            self.assertFalse(shim.exists())
            self.assertEqual(environment, before)
            self.assertEqual(dict(os.environ), global_before)
            self.assertTrue(actual.exists())

    def test_context_missing_curl_and_exception_cleanup(self) -> None:
        with (
            patch.object(refresh.shutil, "which", return_value=None),
            self.assertRaisesRegex(refresh.PodRefreshError, "unavailable"),
            refresh.curl_environment({"PATH": ""}),
        ):
            self.fail("must not yield")
        with (
            patch.object(refresh.shutil, "which", return_value=sys.executable),
            self.assertRaisesRegex(RuntimeError, "caller failure"),
            refresh.curl_environment({}) as environment,
        ):
            directory = Path(environment["PATH"].split(os.pathsep)[0])
            raise RuntimeError("caller failure")
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
