"""Tests for the bounded generated-artifact cleanup policy."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools import clean_work


class CleanWorkTests(unittest.TestCase):
    def test_default_plan_keeps_newest_run_window_and_never_widens_to_its_container(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / ".work/react-native-ios-artifact-consumer"
            old = artifact_root / "run-old"
            new = artifact_root / "run-new"
            old.mkdir(parents=True)
            new.mkdir()
            (old / "evidence.log").write_text("old", encoding="utf-8")
            (new / "evidence.log").write_text("new", encoding="utf-8")
            (old / "evidence.log").touch()
            (new / "evidence.log").touch()

            items = clean_work.build_plan(root, keep_runs=1)
            paths = {item.path for item in items}
            self.assertIn(old, paths)
            self.assertNotIn(new, paths)
            # The collector root is a run window, not a `host/` child: a name this policy
            # does not declare must never be removed, so `run-*` children are the unit.
            self.assertNotIn(artifact_root, paths)
            self.assertNotIn(artifact_root / "host", paths)

    def test_large_roots_require_explicit_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / ".work/archive-local"
            cargo = root / ".work/cargo-target"
            archive.mkdir(parents=True)
            cargo.mkdir()
            self.assertEqual(clean_work.build_plan(root), [])
            items = clean_work.build_plan(
                root, include_archive_local=True, include_cargo_target=True
            )
            self.assertEqual({item.path for item in items}, {archive, cargo})

    def test_plan_refuses_negative_retention(self) -> None:
        with self.assertRaises(ValueError):
            clean_work.build_plan(keep_runs=-1)

    def test_a_family_row_windows_every_scene_of_a_lane_by_its_own_uuid(self) -> None:
        # Evidence lanes name a run by its UUID, not by `run-`: one family row must still bound
        # each scene root, or every new scene becomes an unbounded root.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kept, dropped = [], []
            for scene in ("managed-notification", "managed-io"):
                lane = root / f".work/formal-android-{scene}"
                for index, name in enumerate(
                    (
                        "0a0b0c0d-1111-4000-8000-000000000001",
                        "0a0b0c0d-2222-4000-8000-000000000002",
                    )
                ):
                    attempt = lane / name
                    attempt.mkdir(parents=True)
                    (attempt / "result.json").write_text("{}", encoding="utf-8")
                    os.utime(attempt, (1000 + index, 1000 + index))
                    (kept if index else dropped).append(attempt)

            items = clean_work.build_plan(root, keep_runs=1)
            paths = {item.path for item in items}
            for lane_attempt in dropped:
                self.assertIn(lane_attempt, paths)
            for lane_attempt in kept:
                self.assertNotIn(lane_attempt, paths)

    def test_a_run_cited_by_an_authored_document_is_never_removed(self) -> None:
        # Reports cite run ids. Deleting a cited run breaks a citation the tree still makes,
        # so citation outranks any retention window.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane = root / ".work/formal-android-managed-io"
            cited = lane / "0a0b0c0d-aaaa-4000-8000-000000000001"
            uncited = lane / "0a0b0c0d-bbbb-4000-8000-000000000002"
            for index, attempt in enumerate((cited, uncited)):
                attempt.mkdir(parents=True)
                os.utime(attempt, (1000 + index, 1000 + index))
            report = root / "docs/testing/reports/example.md"
            report.parent.mkdir(parents=True)
            report.write_text(f"run `{cited.name}` passed", encoding="utf-8")

            items = clean_work.build_plan(root, keep_runs=0)
            self.assertEqual({item.path for item in items}, {uncited})

    def test_a_one_off_bundle_is_never_reached_inside(self) -> None:
        # Some roots hold one run's files directly. A window over it must not delete the
        # individual files it happens to contain.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / ".work/formal-android-managed-recovery-0a0b0c0d"
            bundle.mkdir(parents=True)
            (bundle / "001-boot-before.json").write_text("{}", encoding="utf-8")
            (bundle / "002-user.json").write_text("{}", encoding="utf-8")

            self.assertEqual(clean_work.build_plan(root, keep_runs=0), [])

    def test_an_evidence_window_never_windows_infrastructure_children(self) -> None:
        # `child_prefix=""` matches the UUID run shape, not "any child": installed toolchains
        # and staged package images live next to evidence roots under `.work`.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lane = root / ".work/formal-android-managed-io"
            for name in (
                "0a0b0c0d-1111-4000-8000-000000000001",
                "0a0b0c0d-2222-4000-8000-000000000002",
            ):
                (lane / name).mkdir(parents=True)
                os.utime(lane / name, (1000, 1000))
            toolchains = root / ".work/toolchains/appium"
            toolchains.mkdir(parents=True)
            os.utime(toolchains, (1000, 1000))

            items = clean_work.build_plan(root, keep_runs=1)
            self.assertEqual(
                {item.path.name for item in items},
                {"0a0b0c0d-1111-4000-8000-000000000001"},
            )


if __name__ == "__main__":
    unittest.main()
