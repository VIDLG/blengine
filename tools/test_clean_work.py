"""Tests for the bounded generated-artifact cleanup policy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import clean_work


class CleanWorkTests(unittest.TestCase):
    def test_default_plan_keeps_newest_run_and_only_targets_generated_host_output(
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
            host = root / "tests/consumers/react-native-internal"
            (host / "ios/Pods").mkdir(parents=True)
            (host / "ios/build").mkdir()
            (host / "src").mkdir()
            (host / "src/App.tsx").write_text("keep", encoding="utf-8")
            legacy = artifact_root / "host"
            legacy.mkdir()

            items = clean_work.build_plan(root, keep_runs=1)
            paths = {item.path for item in items}
            self.assertIn(old, paths)
            self.assertNotIn(new, paths)
            self.assertIn(host / "ios/Pods", paths)
            self.assertIn(host / "ios/build", paths)
            self.assertIn(legacy, paths)
            self.assertNotIn(host / "src", paths)

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


if __name__ == "__main__":
    unittest.main()
