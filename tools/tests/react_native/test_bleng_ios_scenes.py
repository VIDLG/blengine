"""Hardware-free tests for the iOS binding of the shared scene sequence.

The engine's own contract (argument ownership, order validation, scene budgets, stop at the
first failure, the summary line) is pinned in `test_scene_sequence.py`; this file pins only
what the iOS lane adds: its dependency order, the explicit device and the release of this
bundle's own retained App before every scene.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.react_native import run_bleng_ios_scenes as scenes

DEVICE = "00008110-001234567890ABCD"
BUNDLE = "FWVYQ287W5.bleng-rn-host"
ARGUMENTS = ["--device", DEVICE, "--app-id", BUNDLE]


def scene_runner(fail: str | None = None, seen: list[str] | None = None):
    """A producer stand-in that publishes exactly one run directory per scene."""

    def run(argv: list[str]) -> int:
        scene = argv[argv.index("--scene") + 1]
        output = Path(argv[argv.index("--output") + 1])
        (output / f"run-{scene}").mkdir()
        if seen is not None:
            seen.append(scene)
        return 2 if scene == fail else 0

    return run


def release_recorder(calls: list[dict]):
    def release(**kwargs):
        calls.append(kwargs)
        kwargs["output"].mkdir(parents=True, exist_ok=False)

    return release


class SequenceArgumentTests(unittest.TestCase):
    def test_each_scene_gets_the_callers_arguments_plus_its_own_scope(self) -> None:
        actual = scenes.sequence_arguments(
            ARGUMENTS, "notification", Path("/tmp/root/notification")
        )
        self.assertEqual(
            actual,
            [
                *ARGUMENTS,
                scenes.RELEASE_FLAG,
                scenes.RECLAIM_FLAG,
                "--scene",
                "notification",
                "--output",
                "/tmp/root/notification",
            ],
        )

    def test_a_scene_budget_is_stated_once_after_the_callers_arguments(self) -> None:
        self.assertEqual(
            scenes.sequence_arguments(
                ARGUMENTS, "scan", Path("/tmp/x"), timeout=1620.0
            ),
            [
                *ARGUMENTS,
                scenes.RELEASE_FLAG,
                scenes.RECLAIM_FLAG,
                "--timeout",
                "1620.0",
                "--scene",
                "scan",
                "--output",
                "/tmp/x",
            ],
        )
        self.assertNotIn(
            "--timeout", scenes.sequence_arguments(ARGUMENTS, "scan", Path("/tmp/x"))
        )

    def test_a_sequence_cannot_keep_the_app_of_the_scene_it_just_ran(self) -> None:
        # The caller may not weaken the handoff: a retained App would block the next scene,
        # and one the platform restored between the release and the install would block it
        # too. Both flags state the same rule, one before the scene and one at install.
        self.assertNotIn(scenes.RELEASE_FLAG, ARGUMENTS)
        self.assertNotIn(scenes.RECLAIM_FLAG, ARGUMENTS)
        for scene in scenes.SEQUENCE:
            arguments = scenes.sequence_arguments(ARGUMENTS, scene, Path("/tmp/x"))
            self.assertIn(scenes.RELEASE_FLAG, arguments)
            self.assertIn(scenes.RECLAIM_FLAG, arguments)
        explicit = [*ARGUMENTS, scenes.RELEASE_FLAG, scenes.RECLAIM_FLAG]
        arguments = scenes.sequence_arguments(explicit, "scan", Path("/tmp/x"))
        self.assertEqual(arguments.count(scenes.RELEASE_FLAG), 1)
        self.assertEqual(arguments.count(scenes.RECLAIM_FLAG), 1)

    def test_unknown_scene_is_rejected_before_any_output_root_exists(self) -> None:
        with self.assertRaises(ValueError):
            scenes.sequence_arguments([], "not-a-scene", Path("/tmp/x"))

    def test_the_device_comes_from_the_callers_own_flag(self) -> None:
        self.assertEqual(scenes._device(ARGUMENTS), DEVICE)
        self.assertEqual(scenes._device([f"--device={DEVICE}"]), DEVICE)
        with self.assertRaises(scenes.SceneSequenceError):
            scenes._device(["--app-id", BUNDLE])

    def test_the_released_bundle_is_the_one_the_caller_named(self) -> None:
        self.assertEqual(scenes._bundle_id(ARGUMENTS), BUNDLE)
        self.assertEqual(scenes._bundle_id([f"--app-id={BUNDLE}"]), BUNDLE)
        self.assertEqual(scenes._bundle_id(["--device", DEVICE]), scenes.DEFAULT_BUNDLE)


class SequenceRunTests(unittest.TestCase):
    def test_a_failure_stops_the_sequence_and_keeps_the_passed_scenes(self) -> None:
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            output.mkdir()
            outcomes = scenes.run_sequence(
                output=output,
                arguments=ARGUMENTS,
                device=DEVICE,
                bundle_id=BUNDLE,
                run=scene_runner(fail="notification"),
                release=release_recorder(calls),
            )
            self.assertEqual(
                [(scene, code) for scene, code, _ in outcomes],
                [("scan", 0), ("notification", 2)],
            )
            self.assertFalse((output / "indication").exists())
            # Both admitted scenes were released first, and each release names its scene.
            self.assertEqual(
                [call["output"].parent.name for call in calls],
                ["scan", "notification"],
            )
            self.assertEqual([call["device"] for call in calls], [DEVICE] * 2)

    def test_each_scene_runs_after_its_own_release(self) -> None:
        calls: list[dict] = []
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            output.mkdir()
            outcomes = scenes.run_sequence(
                output=output,
                arguments=ARGUMENTS,
                device=DEVICE,
                bundle_id=BUNDLE,
                run=scene_runner(seen=seen),
                release=release_recorder(calls),
            )
            self.assertEqual([scene for scene, _, _ in outcomes], list(scenes.SEQUENCE))
            self.assertEqual(seen, list(scenes.SEQUENCE))
        self.assertEqual(
            [call["output"].parent.name for call in calls],
            list(scenes.SEQUENCE),
        )

    def test_an_existing_scene_root_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            output.mkdir()
            (output / "scan").mkdir()
            with self.assertRaises(FileExistsError):
                scenes.run_sequence(
                    output=output,
                    arguments=ARGUMENTS,
                    device=DEVICE,
                    bundle_id=BUNDLE,
                    run=scene_runner(),
                    release=release_recorder([]),
                )

    def test_every_scene_is_released_before_it_is_admitted(self) -> None:
        # The first scene included: a passing scene keeps its App, and the platform can
        # restore it, so a sequence never assumes the device starts clean.
        calls: list[dict] = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            output.mkdir()
            scenes.run_sequence(
                output=output,
                arguments=ARGUMENTS,
                device=DEVICE,
                bundle_id=BUNDLE,
                run=scene_runner(),
                release=release_recorder(calls),
            )
        self.assertEqual(len(calls), len(scenes.SEQUENCE))
        self.assertEqual(
            [call["bundle_id"] for call in calls], [BUNDLE] * len(scenes.SEQUENCE)
        )


class SequenceEntryTests(unittest.TestCase):
    def report(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = scenes.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_the_entry_refuses_an_existing_sequence_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            output.mkdir()
            with self.assertRaises(scenes.SceneSequenceError):
                self.report(["--output", str(output), *ARGUMENTS])

    def test_the_entry_requires_an_explicit_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            with self.assertRaises(scenes.SceneSequenceError):
                self.report(["--output", str(output), "--app-id", BUNDLE])
            self.assertFalse(output.exists())

    def test_the_entry_rejects_a_per_scene_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            with self.assertRaises(SystemExit):
                self.report(["--output", str(output), *ARGUMENTS, "--scene", "scan"])
            self.assertFalse(output.exists())

    def test_every_scene_in_order_reports_one_passing_sequence(self) -> None:
        seen: list[list[str]] = []
        original = scenes.run_sequence

        def fake_run_sequence(*, output, arguments, device, bundle_id, **kwargs):
            seen.append([argument for argument in scenes.SEQUENCE])
            return [(scene, 0, output / scene) for scene in scenes.SEQUENCE]

        scenes.run_sequence = fake_run_sequence
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "sequence"
                code, stdout, stderr = self.report(
                    ["--output", str(output), *ARGUMENTS]
                )
        finally:
            scenes.run_sequence = original
        self.assertEqual(code, 0)
        self.assertIn("scene_sequence=passed", stdout)
        self.assertIn(f"scenes={','.join(scenes.SEQUENCE)}", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(seen, [list(scenes.SEQUENCE)])

    def test_the_entry_reports_the_failed_scene_and_the_skipped_ones(self) -> None:
        def fake_run_sequence(*, output, arguments, device, **kwargs):
            return [
                ("scan", 0, output / "scan"),
                ("notification", 2, output / "notification"),
            ]

        original = scenes.run_sequence
        scenes.run_sequence = fake_run_sequence
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "sequence"
                code, stdout, stderr = self.report(
                    ["--output", str(output), *ARGUMENTS]
                )
        finally:
            scenes.run_sequence = original
        self.assertEqual(code, 2)
        self.assertIn("scene=scan passed", stdout)
        self.assertIn("scene=notification failed exit=2", stdout)
        self.assertIn(f"skipped={','.join(scenes.SEQUENCE[2:])}", stderr)
        self.assertIn("scene_sequence=failed scene=notification", stderr)


if __name__ == "__main__":
    unittest.main()
