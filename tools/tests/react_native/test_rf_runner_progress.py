"""One blocking host run must stay observable: phase lines, a live heartbeat, a journal."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import final

from tools.react_native.rf_runner import (
    JOURNAL_FLAG,
    JOURNAL_NAME,
    ProgressReporter,
    ReportedObservations,
    add_journal_argument,
    phase_scope,
    reporter,
)

WAIT_SECONDS = 5.0
# Two heartbeat intervals plus scheduling slack: long enough for an in-flight tick to
# land, short enough to keep the suite quick.
SETTLE_SECONDS = 0.05


def await_text(stream: io.StringIO, needle: str, timeout: float = WAIT_SECONDS) -> bool:
    """Return whether the ticker wrote the line before the bounded wait expired."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in stream.getvalue():
            return True
        time.sleep(0.005)
    return False


@final
class PhaseReportingTests(unittest.TestCase):
    def test_phase_reports_started_then_passed_and_never_swallows(self) -> None:
        output = io.StringIO()
        now = [0.0]
        progress = ProgressReporter("io", stream=output, clock=lambda: now[0])
        with progress.phase("fixture.start", target="nrf52840"):
            now[0] = 7.0
        with (
            self.assertRaisesRegex(ValueError, "peer refused"),
            progress.phase("scene"),
        ):
            raise ValueError("peer refused")
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "rn_rf scenario=io phase=fixture.start status=started target=nrf52840",
                (
                    "rn_rf scenario=io phase=fixture.start status=passed "
                    "phase_elapsed=7s target=nrf52840"
                ),
                "rn_rf scenario=io phase=scene status=started",
                (
                    "rn_rf scenario=io phase=scene status=failed phase_elapsed=0s "
                    "error=ValueError:peer refused"
                ),
            ],
        )

    def test_phase_rejects_an_empty_name_before_any_output(self) -> None:
        output = io.StringIO()
        progress = ProgressReporter("io", stream=output)
        with (
            self.assertRaisesRegex(ValueError, "nonempty phase name"),
            progress.phase(""),
        ):
            pass
        self.assertEqual(output.getvalue(), "")

    def test_phase_scope_is_transparent_without_and_with_a_reporter(self) -> None:
        with phase_scope(None, "scene", kind="cancellation"):
            pass
        output = io.StringIO()
        progress = ProgressReporter("io", stream=output, clock=lambda: 0.0)
        with phase_scope(progress, "scene", kind="cancellation"):
            pass
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "rn_rf scenario=io phase=scene status=started kind=cancellation",
                (
                    "rn_rf scenario=io phase=scene status=passed "
                    "phase_elapsed=0s kind=cancellation"
                ),
            ],
        )

    def test_a_closed_stream_cannot_abort_the_run(self) -> None:
        output = io.StringIO()
        progress = ProgressReporter("io", stream=output)
        output.close()
        with progress.phase("scene"):
            pass


@final
class ReportedObservationsTests(unittest.TestCase):
    def test_each_boundary_reports_the_segment_that_reached_it(self) -> None:
        output = io.StringIO()
        now = [0.0]
        progress = ProgressReporter("cancellation", stream=output, clock=lambda: now[0])
        journal = ReportedObservations(progress, clock=lambda: now[0])
        journal.append("idle")
        now[0] = 2.5
        journal.append("read:queued")
        self.assertEqual(journal, ["idle", "read:queued"])
        self.assertEqual(json.loads(json.dumps(journal)), ["idle", "read:queued"])
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "rn_rf scenario=cancellation marker=idle since_previous=0.0s",
                ("rn_rf scenario=cancellation marker=read:queued since_previous=2.5s"),
            ],
        )
        self.assertEqual(progress.last_marker, "read:queued")

    def test_a_journal_without_a_reporter_still_records_every_observation(
        self,
    ) -> None:
        journal = ReportedObservations()
        for observation in ("idle", "started"):
            journal.append(observation)
        self.assertEqual(journal, ["idle", "started"])


@final
class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_repeats_only_the_open_phase(self) -> None:
        output = io.StringIO()
        progress = ProgressReporter(
            "cancellation", stream=output, heartbeat_seconds=0.01
        )
        with progress.start():
            with progress.phase("automation.session"):
                self.assertTrue(await_text(output, "waiting=automation.session"))
            self.assertIn("phase=automation.session status=passed", output.getvalue())
            time.sleep(SETTLE_SECONDS)
            closed = output.getvalue().count("waiting=automation.session")
            self.assertGreaterEqual(closed, 1)
            time.sleep(SETTLE_SECONDS * 3)
            self.assertEqual(
                output.getvalue().count("waiting=automation.session"), closed
            )

    def test_ticker_writes_nothing_without_an_open_phase(self) -> None:
        output = io.StringIO()
        progress = ProgressReporter("io", stream=output, heartbeat_seconds=0.01)
        with progress.start(), progress.phase("scene"):
            self.assertTrue(await_text(output, "waiting=scene"))
        idle = output.getvalue()
        progress.start()
        try:
            progress.heartbeat()
            time.sleep(SETTLE_SECONDS * 2)
        finally:
            progress.stop()
        self.assertEqual(output.getvalue(), idle)

    def test_start_is_one_shot_and_stop_is_idempotent(self) -> None:
        progress = ProgressReporter(
            "io", stream=io.StringIO(), heartbeat_seconds=0.01, auto_heartbeat=True
        )
        with progress, self.assertRaisesRegex(RuntimeError, "one-shot"):
            progress.start()
        progress.stop()

    def test_auto_heartbeat_is_opt_in(self) -> None:
        progress = ProgressReporter("io", stream=io.StringIO())
        self.assertIs(progress.__enter__(), progress)
        self.assertIsNone(progress._ticker)
        progress.__exit__(None, None, None)

    def test_heartbeat_seconds_must_be_positive(self) -> None:
        for value in (0.0, -1.0):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError, "heartbeat_seconds must be positive"
                ),
            ):
                ProgressReporter("io", stream=io.StringIO(), heartbeat_seconds=value)


@final
class JournalTests(unittest.TestCase):
    """The same events as JSON lines, for a harness that polls a file instead of a log."""

    def path(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory())) / JOURNAL_NAME

    def lines(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_every_event_is_also_one_json_line(self) -> None:
        path = self.path()
        progress = ProgressReporter("io", stream=io.StringIO(), journal=path)
        try:
            with progress.phase("fixture.start", target="dk-a"):
                pass
            progress.marker("scan-submitted")
        finally:
            progress.stop()
        events = self.lines(path)
        self.assertEqual(
            [(event.get("phase"), event.get("status")) for event in events],
            [("fixture.start", "started"), ("fixture.start", "passed"), (None, None)],
        )
        self.assertEqual(events[0]["target"], "dk-a")
        self.assertEqual(events[-1]["marker"], "scan-submitted")
        self.assertTrue(all(event["scenario"] == "io" for event in events))
        self.assertTrue(all(type(event["at"]) is float for event in events))
        self.assertEqual(ProgressReporter("io", stream=io.StringIO())._journal, None)

    def test_the_journal_is_created_where_the_evidence_root_will_be(self) -> None:
        # A first run into a new root opens the journal before the producer creates the root.
        root = self.path().parent / "runs" / "deeper"
        progress = ProgressReporter(
            "io", stream=io.StringIO(), journal=root / JOURNAL_NAME
        )
        try:
            progress.marker("first")
        finally:
            progress.stop()
        self.assertEqual(
            [event["marker"] for event in self.lines(root / JOURNAL_NAME)], ["first"]
        )

    def test_a_journal_that_cannot_take_a_line_is_dropped_and_never_stops_the_run(
        self,
    ) -> None:
        stream = io.StringIO()
        # A directory where a file was named cannot be opened as a journal.
        directory = self.path()
        directory.mkdir()
        progress = ProgressReporter("io", stream=stream, journal=directory)
        self.assertIsNone(progress._journal)

        broken = io.StringIO()

        def refuse(text: str) -> int:
            raise OSError("disk full")

        broken.write = refuse  # type: ignore[method-assign]
        progress._journal = broken  # type: ignore[assignment]
        progress.marker("still reported")
        self.assertIsNone(progress._journal)
        self.assertIn("marker=still reported", stream.getvalue())
        progress.stop()

    def test_events_append_so_a_sequence_shares_one_file(self) -> None:
        path = self.path()
        for scenario in ("scan", "io"):
            progress = ProgressReporter(scenario, stream=io.StringIO(), journal=path)
            try:
                progress.marker("phase")
            finally:
                progress.stop()
        self.assertEqual(
            [event["scenario"] for event in self.lines(path)], ["scan", "io"]
        )

    def test_stop_closes_the_journal_and_later_events_are_not_written(self) -> None:
        path = self.path()
        progress = ProgressReporter("io", stream=io.StringIO(), journal=path)
        progress.marker("before")
        progress.stop()
        progress.marker("after")
        self.assertEqual([event["marker"] for event in self.lines(path)], ["before"])

    def test_the_factory_journals_beside_the_output_unless_told_otherwise(self) -> None:
        root = self.path().parent / "runs"
        named = self.path().parent / "named.jsonl"
        for journal, expected in ((None, root / JOURNAL_NAME), (named, named)):
            with self.subTest(journal=journal):
                progress = reporter(
                    "scan", prefix="formal_rn", output=root, journal=journal
                )
                try:
                    self.assertTrue(progress.auto_heartbeat)
                    self.assertTrue(progress._journal is not None)
                finally:
                    progress.stop()
                self.assertTrue(expected.is_file())
        bare = reporter("scan", prefix="formal_rn")
        self.assertIsNone(bare._journal)

    def test_every_producer_declares_the_same_flag(self) -> None:
        parser = argparse.ArgumentParser()
        add_journal_argument(parser)
        self.assertEqual(
            parser.parse_args([JOURNAL_FLAG, "custom.jsonl"]).progress_journal,
            Path("custom.jsonl"),
        )
        self.assertIsNone(parser.parse_args([]).progress_journal)


if __name__ == "__main__":
    unittest.main()
