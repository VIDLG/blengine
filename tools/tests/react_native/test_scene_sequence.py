"""Hardware-free tests for the shared scene sequence engine and its Android binding.

A sequence is the one command that proves a platform lane, so its rules are pinned here
without a device: it owns its argument contract, it refuses a bad order before any evidence
root exists, it hands every scene the budget that scene itself declares, it stops at the
first failure and it never reuses an evidence root.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.react_native import run_bleng_android_scenes as android
from tools.react_native import run_bleng_ios_scenes as ios
from tools.react_native import scene_sequence as sequence
from tools.react_native.bleng_android_scan import MINIMUM_SCENARIO_SECONDS
from tools.react_native.bleng_scan_evidence import SCENES

ARGUMENTS = ["--device", "3ac5cc09", "--use-root"]


def recorder(outcomes: dict[str, int], calls: list[list[str]]):
    """A producer stand-in: records each scene's exact argv and answers from `outcomes`."""

    def run(argv: list[str]) -> int:
        calls.append(argv)
        return outcomes[argv[argv.index("--scene") + 1]]

    return run


class ArgumentContractTests(unittest.TestCase):
    def test_a_flag_is_read_in_either_of_argparse_s_two_spellings(self) -> None:
        self.assertEqual(
            sequence.flag_value(["--device", "a", "--use-root"], "--device"), "a"
        )
        self.assertEqual(sequence.flag_value(["--device=a"], "--device"), "a")
        self.assertIsNone(sequence.flag_value(["--use-root"], "--device"))
        # A flag given without a value is the producer's error to report, not a blank one.
        self.assertIsNone(sequence.flag_value(["--device"], "--device"))

    def test_the_sequence_owns_its_root_and_passes_everything_else_through(
        self,
    ) -> None:
        output, arguments = sequence.parse(
            ["--output", "runs", "--device=a", "--use-root", "--timeout", "480"],
            description="test",
        )
        self.assertEqual(output, Path("runs").absolute())
        self.assertEqual(arguments, ["--device=a", "--use-root", "--timeout", "480"])

    def test_a_per_scene_selection_or_output_cannot_be_smuggled_through(self) -> None:
        for smuggled in (
            ["--scene", "scan"],
            ["--scene=scan"],
            ["--output", "elsewhere"],
            ["--output=elsewhere"],
            ["--progress-journal", "elsewhere.jsonl"],
            ["--progress-journal=elsewhere.jsonl"],
        ):
            with self.subTest(smuggled=smuggled), self.assertRaises(SystemExit):
                sequence.parse(["--output", "runs", *smuggled], description="test")

    def test_the_sequence_root_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            sequence.create_root(output)
            with self.assertRaises(sequence.SceneSequenceError):
                sequence.create_root(output)


class OrderTests(unittest.TestCase):
    def test_an_unknown_or_repeated_scene_is_refused(self) -> None:
        for order in (("scan", "nope"), ("scan", "scan")):
            with self.subTest(order=order), self.assertRaises(ValueError):
                sequence.require_order(order)
        self.assertEqual(sequence.require_order(("scan", "io")), ("scan", "io"))

    def test_a_bad_order_creates_no_evidence_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            for order in (("scan", "nope"), ("scan", "scan")):
                with self.subTest(order=order), self.assertRaises(ValueError):
                    sequence.run_sequence(
                        output=output,
                        arguments=ARGUMENTS,
                        order=order,
                        run=recorder({}, []),
                    )
            self.assertFalse(output.exists())


class SceneBudgetTests(unittest.TestCase):
    def test_a_scene_without_a_declared_minimum_leaves_the_caller_in_charge(
        self,
    ) -> None:
        self.assertIsNone(
            sequence.scene_budget(SCENES["scan"], None, preparation_seconds=120)
        )
        self.assertEqual(
            sequence.scene_budget(SCENES["scan"], 480, preparation_seconds=120), 480
        )

    def test_a_repeating_scene_is_raised_to_its_own_declared_floor(self) -> None:
        ten = SCENES["recovery-ten"]
        self.assertEqual(
            sequence.scene_budget(ten, None, preparation_seconds=120),
            ten.min_seconds + 120,
        )
        self.assertEqual(
            sequence.scene_budget(ten, 480, preparation_seconds=120),
            ten.min_seconds + 120,
        )

    def test_a_caller_may_declare_more_than_a_scene_never_less(self) -> None:
        ten = SCENES["recovery-ten"]
        generous = ten.min_seconds + 120 + 600
        self.assertEqual(
            sequence.scene_budget(ten, generous, preparation_seconds=120), generous
        )


class RunTests(unittest.TestCase):
    def sequence_run(self, outcomes, **changes):
        calls: list[list[str]] = []
        values = {
            "output": Path(self.enterContext(tempfile.TemporaryDirectory()))
            / "sequence",
            "arguments": ARGUMENTS,
            "order": ("scan", "notification", "io"),
            "run": recorder(outcomes, calls),
        }
        values.update(changes)
        return sequence.run_sequence(**values), calls, values["output"]

    def argv(self, output: Path, name: str, *extra: str) -> list[str]:
        """One scene's exact argv: the caller's, any raised budget, the sequence's journal."""
        return [
            *ARGUMENTS,
            *extra,
            "--progress-journal",
            str(output / "progress.jsonl"),
            "--scene",
            name,
            "--output",
            str(output / name),
        ]

    def events(self, output: Path) -> list[dict]:
        """The sequence's journal, one object per line."""
        text = (output / "progress.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_every_scene_runs_once_in_order_under_its_own_exclusive_root(self) -> None:
        outcomes, calls, output = self.sequence_run(
            {"scan": 0, "notification": 0, "io": 0}
        )
        self.assertEqual(
            [(scene.scene, scene.code, scene.root) for scene in outcomes],
            [
                ("scan", 0, output / "scan"),
                ("notification", 0, output / "notification"),
                ("io", 0, output / "io"),
            ],
        )
        self.assertEqual(
            calls,
            [self.argv(output, name) for name in ("scan", "notification", "io")],
        )
        with self.assertRaises(FileExistsError):
            sequence.run_sequence(
                output=output,
                arguments=ARGUMENTS,
                order=("scan", "notification", "io"),
                run=recorder({"scan": 0}, []),
            )

    def test_the_first_failure_ends_the_sequence_and_keeps_what_passed(self) -> None:
        outcomes, calls, _ = self.sequence_run({"scan": 0, "notification": 2, "io": 0})
        self.assertEqual(
            [(scene.scene, scene.code) for scene in outcomes],
            [("scan", 0), ("notification", 2)],
        )
        self.assertEqual(len(calls), 2)

    def test_the_platform_hook_precedes_every_admission_including_the_first(
        self,
    ) -> None:
        output = Path(self.enterContext(tempfile.TemporaryDirectory())) / "sequence"
        events: list[str] = []

        def before_scene(scene: str, root: Path) -> None:
            self.assertEqual(root, output / scene)
            self.assertTrue(root.is_dir())  # the hook publishes under the scene's root
            events.append("release:" + scene)

        def run(argv: list[str]) -> int:
            events.append("run:" + argv[argv.index("--scene") + 1])
            return 0

        sequence.run_sequence(
            output=output,
            arguments=ARGUMENTS,
            order=("scan", "notification", "io"),
            run=run,
            before_scene=before_scene,
        )
        self.assertEqual(
            events,
            [
                "release:scan",
                "run:scan",
                "release:notification",
                "run:notification",
                "release:io",
                "run:io",
            ],
        )

    def test_a_hook_failure_stops_the_sequence_before_that_scene_is_run(self) -> None:
        def before_scene(scene: str, root: Path) -> None:
            if scene == "notification":
                raise RuntimeError("release failed")

        with self.assertRaisesRegex(RuntimeError, "release failed"):
            self.sequence_run({"scan": 0, "notification": 0}, before_scene=before_scene)

    def test_each_scene_is_handed_its_own_budget_as_the_last_timeout(self) -> None:
        _, calls, output = self.sequence_run(
            {"scan": 0, "recovery-ten": 0},
            order=("scan", "recovery-ten"),
            preparation_seconds=MINIMUM_SCENARIO_SECONDS,
        )
        ten = str(SCENES["recovery-ten"].min_seconds + MINIMUM_SCENARIO_SECONDS)
        # A scene with no declared minimum keeps the producer's own default.
        self.assertEqual(calls[0], self.argv(output, "scan"))
        # The repeating scene's floor is appended, where argparse reads it last.
        self.assertEqual(calls[1], self.argv(output, "recovery-ten", "--timeout", ten))

    def test_a_declared_timeout_is_a_floor_raised_only_where_a_scene_needs_it(
        self,
    ) -> None:
        _, calls, output = self.sequence_run(
            {"scan": 0, "recovery-ten": 0},
            arguments=[*ARGUMENTS, "--timeout=480"],
            order=("scan", "recovery-ten"),
            preparation_seconds=MINIMUM_SCENARIO_SECONDS,
        )
        ten = str(SCENES["recovery-ten"].min_seconds + MINIMUM_SCENARIO_SECONDS)
        self.assertEqual(
            calls,
            [
                # The caller's own budget is left exactly as stated for the scene it fits.
                self.argv(output, "scan", "--timeout=480"),
                self.argv(output, "recovery-ten", "--timeout=480", "--timeout", ten),
            ],
        )

    def test_a_non_numeric_timeout_is_refused_before_any_root_exists(self) -> None:
        output = Path(self.enterContext(tempfile.TemporaryDirectory())) / "sequence"
        with self.assertRaises(sequence.SceneSequenceError):
            sequence.run_sequence(
                output=output,
                arguments=[*ARGUMENTS, "--timeout", "soon"],
                order=("scan",),
                run=recorder({"scan": 0}, []),
            )
        self.assertFalse(output.exists())

    def test_one_journal_covers_the_whole_sequence_and_always_ends(self) -> None:
        # A harness polls this one file; its last line says whether the run is over.
        _, _, output = self.sequence_run({"scan": 0, "notification": 0, "io": 0})
        events = self.events(output)
        self.assertEqual(
            [(e["event"], e.get("scene"), e.get("status")) for e in events],
            [
                ("scene", "scan", "started"),
                ("scene", "scan", "passed"),
                ("scene", "notification", "started"),
                ("scene", "notification", "passed"),
                ("scene", "io", "started"),
                ("scene", "io", "passed"),
                ("finished", None, "passed"),
            ],
        )
        finished = events[-1]
        self.assertEqual(
            (finished["passed"], finished["failed"], finished["skipped"]),
            (["scan", "notification", "io"], [], []),
        )
        self.assertTrue(all(e["scenario"] == "sequence" for e in events))
        self.assertTrue(all(type(e["at"]) is float for e in events))

    def test_a_failed_scene_is_journalled_with_what_passed_and_what_was_skipped(
        self,
    ) -> None:
        _, _, output = self.sequence_run({"scan": 0, "notification": 2, "io": 0})
        finished = self.events(output)[-1]
        self.assertEqual(
            (
                finished["status"],
                finished["passed"],
                finished["failed"],
                finished["skipped"],
            ),
            ("failed", ["scan"], ["notification"], ["io"]),
        )

    def test_an_interrupted_run_still_closes_the_journal(self) -> None:
        output = Path(self.enterContext(tempfile.TemporaryDirectory())) / "sequence"

        def interrupted(argv: list[str]) -> int:
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            sequence.run_sequence(
                output=output,
                arguments=ARGUMENTS,
                order=("scan", "io"),
                run=interrupted,
            )
        finished = self.events(output)[-1]
        self.assertEqual(
            (finished["event"], finished["status"], finished["error"]),
            ("finished", "interrupted", "KeyboardInterrupt"),
        )

    def test_the_journal_is_handed_to_every_scene_and_lives_outside_their_roots(
        self,
    ) -> None:
        _, calls, output = self.sequence_run({"scan": 0, "notification": 0, "io": 0})
        # One file for all the scenes' own events, beside the roots that get sealed.
        self.assertEqual(
            {command[command.index("--progress-journal") + 1] for command in calls},
            {str(output / "progress.jsonl")},
        )
        self.assertEqual(
            sorted(path.name for path in output.iterdir()),
            ["io", "notification", "progress.jsonl", "scan"],
        )


class ReportTests(unittest.TestCase):
    def report(self, outcomes, order=("scan", "notification", "io")):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = sequence.report(outcomes, order, Path("/runs"), out=stdout, err=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_passing_sequence_names_every_scene_in_order(self) -> None:
        outcomes = [
            sequence.SceneOutcome(name, 0, Path("/runs") / name)
            for name in ("scan", "notification", "io")
        ]
        code, stdout, stderr = self.report(outcomes)
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.splitlines(),
            [
                "scene=scan passed result_root=/runs/scan",
                "scene=notification passed result_root=/runs/notification",
                "scene=io passed result_root=/runs/io",
                "scene_sequence=passed scenes=scan,notification,io root=/runs",
            ],
        )
        self.assertEqual(stderr, "")

    def test_a_failure_names_the_scene_the_passes_and_what_was_skipped(self) -> None:
        outcomes = [
            sequence.SceneOutcome("scan", 0, Path("/runs/scan")),
            sequence.SceneOutcome("notification", 2, Path("/runs/notification")),
        ]
        code, stdout, stderr = self.report(outcomes)
        self.assertEqual(code, 2)
        self.assertIn("scene=notification failed exit=2", stdout)
        self.assertEqual(
            stderr.splitlines(),
            [
                "scene_sequence=stopped skipped=io",
                "scene_sequence=failed scene=notification passed=scan root=/runs/notification",
            ],
        )

    def test_the_skipped_scenes_follow_the_order_actually_requested(self) -> None:
        outcomes = [sequence.SceneOutcome("io", 2, Path("/runs/io"))]
        _, _, stderr = self.report(outcomes, order=("scan", "io"))
        self.assertIn("skipped=scan", stderr)
        self.assertIn("passed=none", stderr)


class OrderVocabularyTests(unittest.TestCase):
    """One declared order for both platforms: a scene cannot be forgotten on one of them."""

    def test_the_shared_order_is_every_managed_scene_once(self) -> None:
        # A scene added to the vocabulary without a place in the order fails here, not on a
        # device where it would simply never be run.
        self.assertEqual(set(sequence.ORDER), set(SCENES))
        self.assertEqual(len(sequence.ORDER), len(set(sequence.ORDER)))

    def test_both_platforms_run_exactly_the_shared_order(self) -> None:
        self.assertIs(android.SEQUENCE, sequence.ORDER)
        self.assertIs(ios.SEQUENCE, sequence.ORDER)

    def test_the_shared_order_is_the_dependency_order(self) -> None:
        position = {name: index for index, name in enumerate(sequence.ORDER)}
        # Discovery before subscription before the read/write path ...
        self.assertLess(position["scan"], position["notification"])
        self.assertLess(position["scan"], position["indication"])
        self.assertLess(position["indication"], position["io"])
        # ... the normal path before the fault-armed scenes ...
        self.assertLess(position["io"], position["link-facts"])
        self.assertLess(position["link-facts"], position["disconnect"])
        self.assertLess(position["disconnect"], position["descriptor-io"])
        self.assertLess(position["descriptor-io"], position["devices-query"])
        self.assertLess(position["devices-query"], position["devices-kinds"])
        self.assertLess(position["devices-kinds"], position["connect-failure"])
        self.assertLess(position["connect-failure"], position["auto-connect"])
        self.assertLess(position["auto-connect"], position["cancellation"])
        self.assertLess(position["io"], position["disconnect"])
        self.assertLess(position["disconnect"], position["connect-failure"])
        self.assertLess(position["connect-failure"], position["cancellation"])
        self.assertLess(position["io"], position["cancellation"])
        for fault in (
            "write-reject",
            "ccc-enable-reject",
            "ccc-disable-reject",
            "passive-disconnect",
        ):
            self.assertLess(position["io"], position[fault])
        # ... and one recovery cycle before ten.
        self.assertLess(position["passive-disconnect"], position["recovery"])
        self.assertLess(position["recovery"], position["recovery-ten"])


class AndroidBindingTests(unittest.TestCase):
    def test_android_admits_a_scene_with_no_platform_hook(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            android.run_sequence(
                output=Path(directory) / "sequence",
                arguments=ARGUMENTS,
                scenes=("scan", "recovery-ten"),
                run=recorder({"scan": 0, "recovery-ten": 0}, calls),
            )
        self.assertNotIn("--timeout", calls[0])
        self.assertEqual(
            calls[1][calls[1].index("--timeout") + 1],
            str(SCENES["recovery-ten"].min_seconds + MINIMUM_SCENARIO_SECONDS),
        )

    def entry(self, argv, outcomes):
        stdout, stderr = io.StringIO(), io.StringIO()
        original = android.run_sequence
        seen: list[dict] = []

        def fake(**kwargs):
            seen.append(kwargs)
            return [
                sequence.SceneOutcome(name, code, kwargs["output"] / name)
                for name, code in outcomes
            ]

        android.run_sequence = fake
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = android.main(argv)
        finally:
            android.run_sequence = original
        return code, stdout.getvalue(), stderr.getvalue(), seen

    def test_the_android_entry_passes_the_callers_arguments_through_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            code, stdout, stderr, seen = self.entry(
                ["--output", str(output), *ARGUMENTS],
                [("scan", 0), ("notification", 2)],
            )
        self.assertEqual(code, 2)  # the first failure ends the sequence
        self.assertEqual(seen[0]["arguments"], ARGUMENTS)
        self.assertTrue(seen[0]["output"].is_absolute())
        self.assertIn("scene=scan passed", stdout)
        self.assertIn("scene_sequence=failed scene=notification passed=scan", stderr)
        self.assertIn(
            "skipped=indication,background,io,link-facts,disconnect,descriptor-io,devices-query,"
            "devices-kinds,adapter-control,connect-failure,auto-connect,cancellation,",
            stderr,
        )

    def test_the_android_entry_passes_only_when_every_scene_passed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            code, stdout, _, _ = self.entry(
                ["--output", str(output), *ARGUMENTS],
                [(name, 0) for name in android.SEQUENCE],
            )
        self.assertEqual(code, 0)
        self.assertIn(
            f"scene_sequence=passed scenes={','.join(android.SEQUENCE)}", stdout
        )

    def test_the_android_entry_refuses_an_existing_root_or_a_per_scene_selection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sequence"
            with self.assertRaises(SystemExit):
                android.main(["--output", str(output), *ARGUMENTS, "--scene", "scan"])
            self.assertFalse(output.exists())
            output.mkdir()
            with self.assertRaises(sequence.SceneSequenceError):
                android.main(["--output", str(output), *ARGUMENTS])


if __name__ == "__main__":
    unittest.main()
