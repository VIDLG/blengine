"""A fake Android phone for the scene driver's device protocol, and a harness over it.

This is test support, never evidence: it answers the few adb shapes
`run_android_scene_driver` uses from scripted state, so the lane's file contract — handoff
request and acknowledgement, receipt, hierarchy, the done file published last — can be driven
without hardware. Synthetic contract tests and device-observed regressions both use it, which
is why it is here and not inside either test module.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Self
from unittest.mock import patch

from tools.android import adb
from tools.react_native import run_android_scene_driver as driver

SERIAL = "3ac5cc09"
APP_ID = "com.blehubblenghost"
ACTIVITY = "com.blehubblenghost.MainActivity"
STEPS: tuple[dict[str, str], ...] = ({"op": "activate"}, {"op": "tap", "id": "start"})
DRIVER_PID = 9002
HOST_PID = 7001


def marker(message: str, pid: int = DRIVER_PID) -> str:
    return f"I/{driver.LOG_TAG}({pid}): {message}"


class Process:
    """What `adb.start_command` returns: a pipe to read, an exit to poll, a stop to obey."""

    def __init__(self) -> None:
        read, self._write = os.pipe()
        self.stdout = io.BufferedReader(io.FileIO(read, "rb"))
        self.returncode: int | None = None
        # Not a process-group leader, so the lane signals this fake directly.
        self.pid = 0
        self._lock = threading.Lock()

    def feed(self, line: str) -> None:
        with self._lock:
            if self._write is None:
                return
            try:
                os.write(self._write, (line + "\n").encode())
            except OSError:
                self._write = None

    def _end(self, code: int, output: bytes = b"") -> None:
        with self._lock:
            if self._write is None:
                return
            if output:
                os.write(self._write, output)
            os.close(self._write)
            self._write = None
        self.returncode = code

    def finish(self, code: int, output: bytes) -> None:
        """The command ends by itself, with this console output."""
        self._end(code, output)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self._end(-15)

    def kill(self) -> None:
        self._end(-9)

    def wait(self, timeout: float | None = None) -> int:
        return -15 if self.returncode is None else self.returncode


class Phone:
    """The slice of an Android device this lane talks to."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        # Names that stay in the directory although `rm` answers success (recreated by a
        # process that outlived its run); the lane's readback is what catches these.
        self.survive_removal: set[str] = set()
        # Once a plan was pushed, every `rm` fails: cleanup cannot do its work.
        self.fail_removal = False
        # Logcat lines in device order. Whatever exists when the stream starts is replayed.
        self.lines: list[str] = []
        self.commands: list[list[str]] = []
        self.behaviour: Callable[[Phone, Process], None] = publish_passing_scene
        # What the host last pushed; the fake driver reads it like the real one does.
        self.plan: str | None = None
        # The App's Activity can keep the instrumentation process alive after the scene.
        self.resident = False
        self._lock = threading.Lock()
        self._fed = 0

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    @property
    def fed(self) -> int:
        """How many logcat lines the stream has delivered to the host."""
        return self._fed

    def run_command(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: float,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert 0 < timeout <= 60, "a device command must keep a short budget"
        code, output = self._answer(command[3:])
        if check and code:
            raise adb.AdbError(f"exit={code}: {output}")
        return subprocess.CompletedProcess(command, code, output)

    def start_command(self, command: list[str], *, cwd: Path | None = None) -> Process:
        self.commands.append(command)
        arguments = command[3:]
        process = Process()
        if arguments[0] == "logcat":
            threading.Thread(target=self._stream, args=(process,), daemon=True).start()
        else:
            assert arguments[:4] == ["shell", "am", "instrument", "-w"], arguments
            threading.Thread(
                target=self.behaviour, args=(self, process), daemon=True
            ).start()
        return process

    def _stream(self, process: Process) -> None:
        """logcat's shape: the buffer is replayed first, in bursts, then live lines follow."""
        replay = self._snapshot()
        # The real replay paused for a tenth of a second three markers before its end.
        for line in replay[:-3]:
            process.feed(line)
        time.sleep(0.3)
        while process.poll() is None:
            for line in self._snapshot()[self._fed :]:
                process.feed(line)
                self._fed += 1
            time.sleep(0.005)

    def _snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def _answer(self, arguments: Sequence[str]) -> tuple[int, str]:
        if arguments[0] == "exec-out" and arguments[1] == "cat":
            path = arguments[2]
            name = path.rsplit("/", 1)[1]
            # Observed on the device: a missing file answers exit 0 with the complaint
            # on stdout, which is why the lane checks the payload and not the status.
            return (
                (0, self.files[name].decode())
                if name in self.files
                else (0, f"cat: {path}: No such file or directory\n")
            )
        if arguments[0] == "push":
            self.plan = Path(arguments[1]).read_text()
            return 0, ""
        if arguments[0] != "shell" or arguments[1] == "mkdir":
            return 0, ""
        if arguments[1] == "ls":
            names = sorted(self.files)
            return 0, "".join(name + "\n" for name in names)
        if arguments[1] == "rm":  # the scratch directory, removed as separate argv
            return 0, ""
        if arguments[1].startswith("rm -f "):
            prefixes = [
                item.rsplit("/", 1)[1][:-1] for item in arguments[1].split()[2:]
            ]
            if self.fail_removal and self.plan is not None:
                return 1, "rm: 'scene-done': Operation not permitted\n"
            for name in list(self.files):
                if name not in self.survive_removal and any(
                    name.startswith(prefix) for prefix in prefixes
                ):
                    del self.files[name]
            return 0, ""
        if arguments[1:4] == ["log", "-t", driver.HOST_MARKER_TAG]:
            self.log(f"I/{driver.HOST_MARKER_TAG}({HOST_PID}): {arguments[4]}")
            return 0, ""
        if arguments[1] == "touch":
            self.files[arguments[2].rsplit("/", 1)[1]] = b""
            return 0, ""
        raise AssertionError(f"unexpected device command: {arguments}")


def publish(phone: Phone, name: str, text: str) -> None:
    """The driver's publication shape: written beside, then renamed into place."""
    phone.files[name + ".tmp"] = text.encode()
    del phone.files[name + ".tmp"]
    phone.files[name] = text.encode()


def receipt(status: str, steps: Sequence[dict[str, str]], failure: str = "") -> str:
    return json.dumps(
        {
            "scope": driver.ANDROID_RECEIPT_SCOPE,
            "status": status,
            "primary_error": failure,
            "steps": [
                {"index": index, "step": step, "seconds": 0.01, "observed": {}}
                for index, step in enumerate(steps)
            ],
            "values": {},
        }
    )


def await_logcat(phone: Phone, process: Process) -> None:
    """A real driver is slower than its own markers; the fake waits for the stream."""
    deadline = time.monotonic() + 5
    while phone.fed < len(phone.lines) and process.poll() is None:
        if time.monotonic() > deadline:
            return
        time.sleep(0.002)


def publish_passing_scene(
    phone: Phone, process: Process, *, stop_after: int | None = None
) -> None:
    """A driver run: log each step, publish the receipt, the hierarchy, then the done file."""
    plan = json.loads(phone.plan or "")
    phone.log(marker("scene:" + plan["name"]))
    for index, step in enumerate(plan["steps"]):
        if stop_after is not None and index == stop_after:
            failure = f"scene step {index} {step} failed: PlanError: refused"
            publish(
                phone,
                driver.RECEIPT_FILE,
                receipt("failed", plan["steps"][:index], failure),
            )
            publish(phone, driver.DONE_FILE, "failed\n")
            phone.log(marker("result:failed"))
            await_logcat(phone, process)
            process.finish(1, b"\nFAILURES!!! Tests run: 1, Failures: 1\n")
            return
        phone.log(marker(f"step:{index}:{step['op']}"))
    publish(phone, driver.RECEIPT_FILE, receipt("passed", plan["steps"]))
    phone.log(marker("receipt:/files/" + driver.RECEIPT_FILE))
    publish(phone, driver.HIERARCHY_FILE, "<?xml version='1.0'?><hierarchy />")
    phone.log(marker("hierarchy:/files/" + driver.HIERARCHY_FILE))
    publish(phone, driver.DONE_FILE, "passed\n")
    phone.log(marker("done:/files/" + driver.DONE_FILE))
    phone.log(marker("result:passed"))
    await_logcat(phone, process)
    if phone.resident:
        time.sleep(30)  # the App's Activity keeps the process alive; the host stops it
        return
    process.finish(0, b"\nOK (1 test)\n")


class SceneHarness:
    """The real `run_scene` driven against one fake phone.

    Enter it for the duration of a test: it installs the phone at the adb seam and shortens
    the lane's own polling so a scene completes in milliseconds.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.phone = Phone()
        self._stack = ExitStack()

    def __enter__(self) -> Self:
        self._stack.enter_context(
            patch.object(adb, "run_command", self.phone.run_command)
        )
        self._stack.enter_context(
            patch.object(adb, "start_command", self.phone.start_command)
        )
        self._stack.enter_context(patch.object(driver, "POLL_SECONDS", 0.01))
        self._stack.enter_context(patch.object(driver, "_LINGER_SECONDS", 0.05))
        return self

    def __exit__(self, *interruption: object) -> None:
        self._stack.close()

    def scene(self, **changes) -> dict:
        """Run the two-step plan once and return the published result."""
        arguments = {
            "device": SERIAL,
            "plan_name": "tap",
            "output_root": self.root,
            "timeout": 20,
            "launch": False,
            "app_id": APP_ID,
            "activity": ACTIVITY,
            "test_apk_sha256": "ab" * 32,
            "fresh_install": True,
            "steps": STEPS,
            "cwd": self.root,
        }
        arguments.update(changes)
        return json.loads(driver.run_scene(**arguments).read_text())

    def instrumentations(self) -> list[list[str]]:
        """Every `am instrument` command the lane issued, in order."""
        return [
            command
            for command in self.phone.commands
            if command[3:6] == ["shell", "am", "instrument"]
        ]
