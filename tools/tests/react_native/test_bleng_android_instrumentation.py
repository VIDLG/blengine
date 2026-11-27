"""Hardware-free regressions for the shared Android instrumentation driver.

The marker protocol is the only thing every native lane shares; a tag-matching or
deduplication mistake here would corrupt every scene's routing at once.
"""

from __future__ import annotations

import ast
import io
import itertools
import re
import tempfile
import threading
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from tools.android import adb
from tools.react_native import bleng_android_instrumentation as driver

TAG = "BLEHUB_RN_TEST"


def fed_capture(
    lines: Sequence[str] = (), *, tag: str = TAG, host_tag: str | None = None
) -> driver.MarkerCapture:
    """A capture with no process: its lines are fed through the reader's own path."""
    capture = driver.MarkerCapture.__new__(driver.MarkerCapture)
    capture._tag = tag
    capture._host_tag = host_tag
    capture._sentinel = "run-abc" if host_tag is not None else None
    capture._bounded = host_tag is None
    capture._lock = threading.Lock()
    capture._entries = []
    capture._bytes = 0
    for line in lines:
        capture._keep(line)
    return capture


class MarkerTests(unittest.TestCase):
    def test_brief_tagged_lines_are_parsed_in_order(self):
        capture = fed_capture(
            [
                f"I/{TAG}(11493): started:state=poweredon",
                f"I/{TAG}: scan-submitted",
                f"I/{TAG}(1): subscribed:notification",
            ]
        )
        self.assertEqual(
            capture.stages(),
            ["started:state=poweredon", "scan-submitted", "subscribed:notification"],
        )

    def test_foreign_or_noise_lines_never_become_stages(self):
        capture = fed_capture(
            [f"I/{TAG}_OTHER(1): started", "I/ReactNativeJS(1): hello", ""]
        )
        self.assertEqual(capture.stages(), [])

    def test_a_payload_survives_the_marker_and_the_stage_key(self):
        payload = '{"pid":1,"uid":2}'
        capture = fed_capture([f"I/{TAG}(7): bootstrap:{payload}"])
        self.assertEqual(capture.stages(), [f"bootstrap:{payload}"])
        self.assertEqual(driver.stage_name(f"bootstrap:{payload}"), "bootstrap")


class MarkerBaselineTests(unittest.TestCase):
    """A run's markers begin at the host's sentinel, never at a pause in the replay.

    Observed on a real device: the replay of the previous scene stopped for a tenth of a
    second three markers before its end, so a "the count stopped changing" baseline was
    taken mid-replay and that scene's `result:passed` was read as the new scene's.
    """

    HOST = "BLEHUB_RN_HOST"
    HISTORY = (f"I/{TAG}(9001): step:59:wait", f"I/{TAG}(9001): result:passed")
    RUN = ("scene:write-reject", "result:passed")

    def line(self, message: str, pid: int = 9002) -> str:
        return f"I/{self.HOST}({pid}): {message}"

    def test_history_before_the_sentinel_is_never_this_runs(self):
        capture = fed_capture(
            [
                *self.HISTORY,
                self.line("run-abc"),
                *(f"I/{TAG}(9002): {m}" for m in self.RUN),
            ],
            host_tag=self.HOST,
        )
        self.assertEqual(tuple(capture.stages()), self.RUN)
        self.assertTrue(capture.settled())

    def test_nothing_is_reported_until_the_sentinel_arrives(self):
        capture = fed_capture(self.HISTORY, host_tag=self.HOST)
        self.assertEqual(capture.stages(), [])
        self.assertFalse(capture.settled())
        with (
            patch.object(driver.time, "monotonic", side_effect=[100.0, 100.0]),
            self.assertRaises(TimeoutError),
        ):
            capture.require_baseline(100.0)
        capture._keep(self.line("run-abc"))
        capture.require_baseline(100.0)
        self.assertEqual(capture.stages(), [])

    def test_only_this_runs_exact_sentinel_bounds_the_stream(self):
        capture = fed_capture(
            [
                *self.HISTORY,
                # A sentinel is unique per capture, so a replayed one is never this run's.
                self.line("run-previous", pid=7000),
                *self.HISTORY,
                f"I/{self.HOST}_OTHER(1): run-abc",  # a longer tag is foreign
                self.line("run-zzz"),  # another host's message, not our boundary
            ],
            host_tag=self.HOST,
        )
        self.assertFalse(capture.settled())
        self.assertEqual(capture.stages(), [])

    def test_a_long_replay_cannot_starve_the_run_of_its_marker_budget(self):
        noise = [f"I/{TAG}(9001): step:{index}:read" for index in range(50)]
        run = [f"I/{TAG}(9002): {stage}" for stage in self.RUN]
        with patch.object(driver, "MAX_LOG_BYTES", 200):
            capture = fed_capture(
                [*noise, self.line("run-abc"), *run], host_tag=self.HOST
            )
        self.assertEqual(tuple(capture.stages()), self.RUN)

    def test_the_kept_log_is_verbatim_and_bounded(self):
        capture = fed_capture([f"I/{TAG}(7): bootstrap:x", "I/ReactNativeJS(1): hello"])
        self.assertEqual(capture.log(), "I/BLEHUB_RN_TEST(7): bootstrap:x")
        with patch.object(driver, "MAX_LOG_BYTES", 20):
            capture._keep(f"I/{TAG}(7): " + "y" * 40)
        self.assertEqual(capture.stages(), ["bootstrap:x"])


class BaselineBeforeLaunchTests(unittest.TestCase):
    """A run-scoped capture must have delivered its replay before a test process can log."""

    class Capture:
        def __init__(self, events: list[str], settled: bool = True) -> None:
            self.events, self.settled = events, settled

        def require_baseline(self, deadline: float) -> None:
            self.events.append("baseline")
            driver.time.monotonic()
            if not self.settled:
                raise TimeoutError(
                    "logcat did not replay its buffer before the deadline"
                )

        def stages(self) -> list[str]:
            return ["started"]

    def process(self, events: list[str]):
        class Process:
            # Not a process-group leader, so the driver signals this fake directly.
            pid = 0
            returncode = 0

            def __init__(self) -> None:
                self.stdout = io.BytesIO(b"OK (1 test)\n")

            def poll(self) -> int:
                return 0

        def popen(*args: object, **kwargs: object) -> Process:
            events.append("start")
            return Process()

        return popen

    def test_both_drivers_require_the_baseline_before_the_test_process_starts(
        self,
    ) -> None:
        for run in (
            driver.run_instrumentation,
            driver.run_instrumentation_until_process_exit,
        ):
            with self.subTest(driver=run.__name__):
                events: list[str] = []
                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(
                        driver.adb, "start_command", side_effect=self.process(events)
                    ),
                ):
                    try:
                        run(
                            "serial",
                            {},
                            test_class="com.example.Test",
                            method="scene",
                            **(
                                {"expected_stages": ("started",)}
                                if run is driver.run_instrumentation
                                else {"required_stages": ("started",)}
                            ),
                            capture=self.Capture(events),
                            on_stage=lambda stage: None,
                            advance=lambda: None,
                            deadline=100.0,
                            require_owner=lambda: None,
                            log=Path(directory) / "run.log",
                        )
                    except RuntimeError as error:
                        # The exit-by-the-OS driver rejects a process that ended cleanly;
                        # ordering is what this test is about.
                        self.assertIn("finished cleanly", str(error))
                self.assertEqual(events[:2], ["baseline", "start"])

    def test_an_unsettled_replay_starts_no_test_process(self) -> None:
        events: list[str] = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                driver.adb, "start_command", side_effect=self.process(events)
            ) as popen,
            patch.object(driver.time, "monotonic", side_effect=[100.0, 100.0]),
            self.assertRaises(TimeoutError),
        ):
            driver.run_instrumentation(
                "serial",
                {},
                test_class="com.example.Test",
                method="scene",
                expected_stages=("started",),
                capture=self.Capture(events, settled=False),
                on_stage=lambda stage: None,
                advance=lambda: None,
                deadline=100.0,
                require_owner=lambda: None,
                log=Path(directory) / "run.log",
            )
        popen.assert_not_called()
        self.assertEqual(events, ["baseline"])


class StallTests(unittest.TestCase):
    """A test process that stops making progress must be named, not waited out."""

    class Clock:
        """One monotonic clock, advanced by every sleep instead of by wall time."""

        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += max(seconds, 1.0)

    class QuietProcess:
        """Alive, silent and stage-less: the shape the run budget used to hide."""

        pid = 0
        returncode = None

        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")

        def poll(self) -> None:
            return None

    def test_a_silent_process_is_reported_instead_of_running_out_the_budget(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(driver.adb, "start_command", return_value=self.QuietProcess()),
            patch.object(driver.host_command, "stop", lambda process: None),
            patch.object(driver, "time", self.Clock()),
            self.assertRaisesRegex(RuntimeError, "no stage progress"),
        ):
            driver.run_instrumentation(
                "serial",
                {},
                test_class="com.example.Test",
                method="scene",
                expected_stages=("started",),
                capture=fed_capture([]),
                on_stage=lambda stage: None,
                advance=lambda: None,
                deadline=10_000.0,
                require_owner=lambda: None,
                log=Path(directory) / "run.log",
            )


sources = [
    *Path(driver.__file__).parent.glob("*.py"),
    *Path(adb.__file__).parent.glob("*.py"),
]


def clears_device_log(source: Path) -> bool:
    """Does this module's code pass `logcat -c` to a device, in argv or shell form?"""
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Constant) and re.fullmatch(
            r"logcat\s+-c(?:\s.*)?", str(node.value).strip()
        ):
            return True
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            if any(a == "logcat" and b == "-c" for a, b in itertools.pairwise(values)):
                return True
    return False


class DeviceLogOwnershipTests(unittest.TestCase):
    def test_no_native_lane_clears_the_devices_log(self) -> None:
        # `adb logcat -c` erases every app's diagnostics on the phone. The lanes used it to
        # hide earlier runs' markers from logcat's replay; a capture is scoped by its own
        # sentinel instead, so the clear must not come back. Code only: a comment may name it.
        for source in sources:
            with self.subTest(source=source.name):
                self.assertFalse(
                    clears_device_log(source), f"{source.name} clears the device log"
                )


class AdvanceTests(unittest.TestCase):
    def test_each_stage_advances_the_fixture_exactly_once(self):
        seen: list[str] = []
        applied: list[str] = []
        stream = io.StringIO()
        advanced: set[str] = set()
        markers = [
            "started:state=poweredon",
            "subscribed:notification",
            "disabled",
            "done",
        ]

        def advance() -> None:
            seen.append("drain")

        def on_stage(name: str) -> None:
            applied.append(name)

        driver._advance_fixture(markers, advanced, stream, on_stage, advance)
        # A replay of the same markers must add nothing at all.
        driver._advance_fixture(markers, advanced, stream, on_stage, advance)
        self.assertEqual(applied, ["started", "subscribed", "disabled", "done"])
        self.assertEqual(seen.count("drain"), 4)
        self.assertEqual(stream.getvalue().count("[marker]"), 4)

    def test_a_per_peer_key_keeps_two_links_apart(self):
        applied: list[str] = []
        driver._advance_fixture(
            ["subscribed:0", "subscribed:1"],
            set(),
            io.StringIO(),
            applied.append,
            lambda: None,
            key=lambda stage: stage,
        )
        self.assertEqual(applied, ["subscribed:0", "subscribed:1"])


if __name__ == "__main__":
    unittest.main()
