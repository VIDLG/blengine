"""Keep prefix replay and line-framed coordination schemas separate and scoped."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.react_native import rf_suite_coordination as api


class SuiteCoordinationTests(unittest.TestCase):
    def test_prefix_replay_keeps_flow_segments_and_ready_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markers = [
                "flow-started:index=5:mode=rf-notification",
                "rf:step:notification-enable:passed:commandId=1",
                "rf:step:notification-disable:passed:commandId=2",
                "flow-passed:index=5:mode=rf-notification",
                "flow-started:index=6:mode=rf-indication",
                "rf:step:indication-enable:passed:commandId=3",
                "flow-failed:index=6:mode=rf-indication:detail=synthetic",
                "flow-started:index=7:mode=rf-passive-disconnect",
                "rf:step:notification-enable:passed:commandId=4",
                "rf:step:notification-value:passed:value=ef",
            ]
            text = "\n".join(
                "BLEHUB_NITRO_RUNTIME "
                + (s if s.startswith("rf:") else "rf-lab-suite:" + s)
                for s in markers
            )
            for _ in range(2):
                api.advance_prefix(text, root)
            expected = {
                "flow-5-rf-notification/started",
                "flow-5-rf-notification/subscription-enabled",
                "flow-5-rf-notification/subscription-disabled",
                "flow-5-rf-notification/done",
                "flow-6-rf-indication/started",
                "flow-6-rf-indication/subscription-enabled",
                "flow-6-rf-indication/abort",
                "flow-7-rf-passive-disconnect/started",
                "flow-7-rf-passive-disconnect/subscription-enabled",
                "flow-7-rf-passive-disconnect/connected",
            }
            actual = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            self.assertEqual(set(actual), expected)
            self.assertEqual(set(actual.values()), {b"ready\n"})

    def test_line_adapter_requires_current_flow_and_retains_empty_file_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = api.LineCoordination(root)
            owner.observe_line(
                "rf:step:notification-enable:passed:ignored-before-start"
            )
            self.assertEqual(list(root.iterdir()), [])
            owner.observe_line(
                "console: rf-lab-suite:flow-started:index=5:mode=rf-notification"
            )
            owner.observe_line("rf:step:notification-enable:passed:commandId=1")
            owner.observe_line("rf-lab-suite:flow-passed:index=6:mode=rf-indication")
            directory = root / "flow-5-rf-notification"
            self.assertFalse((directory / "done").exists())
            owner.observe_line("rf:step:notification-disable:passed:commandId=2")
            owner.observe_line("rf-lab-suite:flow-passed:index=5:mode=rf-notification")
            owner.observe_line("rf-lab-suite:flow-started:index=6:mode=rf-indication")
            owner.observe_line(
                "rf-lab-suite:flow-failed:index=5:mode=rf-notification:foreign"
            )
            self.assertFalse((directory / "abort").exists())
            owner.observe_line(
                "rf-lab-suite:flow-failed:index=6:mode=rf-indication:owned"
            )
            self.assertTrue((root / "flow-6-rf-indication/abort").exists())
            self.assertEqual(
                {p.name for p in directory.iterdir()},
                {"started", "subscription-enabled", "subscription-disabled", "done"},
            )
            self.assertTrue(
                all(p.read_bytes() == b"" for p in root.rglob("*") if p.is_file())
            )

    def test_invalid_index_mode_is_rejected_without_creating_a_foreign_directory(
        self,
    ) -> None:
        for index, mode in ((5, "rf-indication"), (10, "rf"), (999, "unknown")):
            for incremental in (False, True):
                with (
                    self.subTest(index=index, mode=mode, incremental=incremental),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    line = f"BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index={index}:mode={mode}"
                    with self.assertRaises(api.RfSuiteCoordinationError):
                        if incremental:
                            api.LineCoordination(root).observe_line(line)
                        else:
                            api.advance_prefix(line, root)
                    self.assertEqual(list(root.iterdir()), [])
