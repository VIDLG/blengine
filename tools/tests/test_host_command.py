"""Contract for the one host command boundary: bounded run, streaming start, bounded stop.

`run` and `start` are also exercised at their callers (`test_adb.py` checks the adb layer
over them). This file pins `stop`, which replaced nine hand-copied terminate/wait/kill
sequences: every lane stops the command it started the same way, and none waits forever.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from tools import host_command

# A command that starts a helper of its own and reports the helper's pid first.
HELPER_STARTED = (
    "import subprocess, sys, time\n"
    "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(helper.pid, flush=True)\n"
    "time.sleep(60)\n"
)


class FakeProcess:
    """The shape of a `Popen` as `stop` uses it, with every signal and wait recorded."""

    pid = 0  # not a process-group leader

    def __init__(self, *, exits_on: str | None = "SIGTERM") -> None:
        self.exits_on = exits_on
        self.exit: int | None = None
        self.signals: list[str] = []
        self.waits: list[float] = []

    def poll(self) -> int | None:
        return self.exit

    def terminate(self) -> None:
        self.signals.append("SIGTERM")
        if self.exits_on == "SIGTERM":
            self.exit = 0

    def kill(self) -> None:
        self.signals.append("SIGKILL")
        if self.exits_on == "SIGKILL":
            self.exit = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(-1.0 if timeout is None else timeout)
        if self.exit is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.exit


class StopTests(unittest.TestCase):
    def test_a_process_that_already_left_is_not_signalled(self) -> None:
        process = FakeProcess()
        process.exit = 0
        host_command.stop(process)
        self.assertEqual((process.signals, process.waits), ([], []))

    def test_a_process_that_obeys_terminate_is_never_killed(self) -> None:
        process = FakeProcess(exits_on="SIGTERM")
        host_command.stop(process, grace=1.5)
        self.assertEqual(process.signals, ["SIGTERM"])
        self.assertEqual(process.waits, [1.5])

    def test_a_process_that_ignores_terminate_is_killed_after_the_grace(self) -> None:
        process = FakeProcess(exits_on="SIGKILL")
        host_command.stop(process, grace=0.25)
        self.assertEqual(process.signals, ["SIGTERM", "SIGKILL"])
        self.assertEqual(process.waits, [0.25, 0.25])

    def test_a_process_that_survives_kill_is_reported_not_waited_on_forever(
        self,
    ) -> None:
        process = FakeProcess(exits_on=None)
        with self.assertRaises(subprocess.TimeoutExpired):
            host_command.stop(process, grace=0.25)
        # Bounded: two grace periods in total, never a wait without a deadline.
        self.assertEqual(process.waits, [0.25, 0.25])


class StopRealProcessTests(unittest.TestCase):
    def test_a_command_that_leads_its_own_group_takes_its_helpers_with_it(self) -> None:
        # An SDK tool starts helpers; signalling the leader alone would leave them running.
        child = host_command.start([sys.executable, "-c", HELPER_STARTED], cwd=None)
        try:
            assert child.stdout is not None
            helper = int(child.stdout.readline())
            host_command.stop(child, grace=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(helper, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail(f"helper {helper} outlived the command that started it")
            self.assertIsNotNone(child.poll())
        finally:
            host_command.stop(child, grace=5)
            if child.stdout is not None:
                child.stdout.close()

    def test_a_leader_that_exits_on_terminate_is_stopped_without_a_kill(self) -> None:
        child = host_command.start(
            [
                sys.executable,
                "-c",
                "import signal, time\nsignal.signal(signal.SIGTERM, lambda *_: exit(0))\nprint('ready', flush=True)\ntime.sleep(60)\n",
            ],
            cwd=None,
        )
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            started = time.monotonic()
            host_command.stop(child, grace=5)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(child.returncode, 0)
        finally:
            host_command.stop(child, grace=5)
            if child.stdout is not None:
                child.stdout.close()


class RunAndStartTests(unittest.TestCase):
    def test_run_merges_stderr_and_returns_the_exit_as_the_answer(self) -> None:
        completed = host_command.run(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
            ],
            cwd=None,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 3)
        # Both streams are in one pipe. Their order is the child's own buffering (stderr is
        # unbuffered, a piped stdout is not), so the contract is presence, not sequence.
        self.assertEqual(sorted(completed.stdout.splitlines()), ["err", "out"])

    def test_start_leads_its_own_session_and_hands_back_its_output(self) -> None:
        process = host_command.start(
            [
                sys.executable,
                "-c",
                "import os; print(os.getpgrp() == os.getpid(), flush=True)",
            ],
            cwd=None,
        )
        try:
            assert process.stdout is not None
            # Read to the end first: the child prints and leaves at once, so stopping it
            # before it has run would win the race and prove nothing.
            self.assertEqual(process.stdout.read().strip(), b"True")
        finally:
            host_command.stop(process, grace=5)
            if process.stdout is not None:
                process.stdout.close()

    def test_a_command_that_cannot_start_is_a_typed_failure(self) -> None:
        for action in ("run", "start"):
            with (
                self.subTest(action=action),
                self.assertRaises(host_command.HostCommandError),
            ):
                if action == "run":
                    host_command.run(["blehub-no-such-tool"], cwd=None, timeout=5)
                else:
                    host_command.start(["blehub-no-such-tool"], cwd=None)


if __name__ == "__main__":
    unittest.main()
