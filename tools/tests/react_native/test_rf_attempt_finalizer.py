"""Protect cleanup/error ordering independently of any platform or RF scenario."""

from __future__ import annotations

import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock

from tools.react_native.rf_attempt_finalizer import finalize_rf_attempt


class AttemptFinalizerTests(unittest.TestCase):
    def test_abort_and_cleanup_failures_do_not_skip_finish_or_replace_primary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                root / "abort"
            ).mkdir()  # Real write failure without globally patching Path.
            primary = RuntimeError("original RF error")
            events: list[str] = []
            published: dict[str, object] = {}

            def stop() -> None:
                events.append("stop")
                raise OSError("owned stop failed")

            def finish(**kwargs: object) -> None:
                events.append("finish")
                published.update(kwargs)
                published["cleanup_errors"] = list(kwargs["cleanup_errors"])
                raise zlib.error("archive finalization failed")

            bundle = Mock(finish=Mock(side_effect=finish))
            report = Mock(side_effect=lambda ok: events.append(f"report:{ok}"))
            finalize_rf_attempt(
                coordination=root,
                bundle=bundle,
                completed=False,
                primary=primary,
                runtime_log=root / "runtime.log",
                stop_owned=stop,
                report=report,
            )
            self.assertEqual(events, ["stop", "finish", "report:False"])
            bundle.finish.assert_called_once()
            self.assertIs(published["primary_error"], primary)
            self.assertFalse(published["completed"])
            self.assertIn("abort publication:", published["cleanup_errors"][0])
            self.assertIn(
                "fixture cleanup: owned stop failed", published["cleanup_errors"]
            )
            notes = " ".join(primary.__notes__)
            for failure in (
                "abort publication:",
                "owned stop failed",
                "archive finalization failed",
            ):
                self.assertIn(failure, notes)

    def test_completed_scenario_cannot_hide_cleanup_or_finalization_failure(
        self,
    ) -> None:
        for point in ("stop", "finish"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stop = Mock(
                    side_effect=OSError("stop failed") if point == "stop" else None
                )
                bundle = Mock(
                    finish=Mock(
                        side_effect=AssertionError("finish failed")
                        if point == "finish"
                        else None
                    )
                )
                report = Mock()
                with self.assertRaisesRegex(RuntimeError, f"{point} failed"):
                    finalize_rf_attempt(
                        coordination=root,
                        bundle=bundle,
                        completed=True,
                        primary=None,
                        runtime_log=root / "runtime.log",
                        stop_owned=stop,
                        report=report,
                    )
                stop.assert_called_once_with()
                bundle.finish.assert_called_once()
                self.assertTrue(bundle.finish.call_args.kwargs["completed"])
                self.assertIsNone(bundle.finish.call_args.kwargs["primary_error"])
                report.assert_called_once_with(False)
                self.assertFalse((root / "abort").exists())

    def test_unowned_early_failure_only_publishes_abort_and_failed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = ValueError("preflight failure")
            bundle, report = Mock(), Mock()
            finalize_rf_attempt(
                coordination=root,
                bundle=bundle,
                completed=False,
                primary=primary,
                runtime_log=root / "runtime.log",
                stop_owned=None,
                report=report,
            )
            self.assertEqual((root / "abort").read_text(), "parent-failed\n")
            bundle.finish.assert_called_once_with(
                completed=False,
                primary_error=primary,
                cleanup_errors=[],
                runtime_log=root / "runtime.log",
            )
            report.assert_called_once_with(False)
