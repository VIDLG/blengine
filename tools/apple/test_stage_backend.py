from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.apple import stage_backend

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "platforms/apple/packaging/Package.swift.in"
)


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class BackendStageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="backend stage ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "platforms/apple/Sources/BleHubApple"
        self.source.mkdir(parents=True)
        (self.source / "Backend.swift").write_text("import BleHubUniFfi\n")
        (self.source / "Nested").mkdir()
        (self.source / "Nested/Ownership.swift").write_text("// ownership\n")
        self.template = self.root / "platforms/apple/packaging/Package.swift.in"
        self.template.parent.mkdir(parents=True)
        shutil.copy2(TEMPLATE, self.template)
        self.uniffi = self.root / '.work/staged/input artifacts/uni "\\(name)\t\n'
        self.uniffi.mkdir(parents=True)
        (self.uniffi / "Package.swift").write_text("// immutable UniFFI package\n")
        (self.uniffi / "BleHubUniFfiNative.xcframework").mkdir()
        (self.uniffi / "BleHubUniFfiNative.xcframework/native.a").write_bytes(
            b"immutable native image"
        )
        (self.uniffi / "BleHubUniFfi.flavor").write_text("production\n")
        self.output = self.root / ".work/staged/backend output"
        root_patch = patch.object(stage_backend, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_render_relative_dependency_and_only_stage_backend_sources(self) -> None:
        tests = self.root / "platforms/apple/Tests"
        tests.mkdir()
        (tests / "Test.swift").write_text("// must not stage\n")
        (self.source / ".DS_Store").write_bytes(b"not Swift source")
        original_input = tree_bytes(self.uniffi)
        original_source = tree_bytes(self.source)

        self.assertEqual(stage_backend.stage(self.uniffi, self.output), self.output)
        manifest = (self.output / "Package.swift").read_text()
        self.assertIn('path: "../input artifacts/uni \\"\\\\(name)\\t\\n"', manifest)
        self.assertIn('name: "BleHubUniFfi"', manifest)
        self.assertIn('package: "BleHubUniFfi"', manifest)
        self.assertEqual(manifest.count(".library("), 1)
        self.assertNotIn(".binaryTarget", manifest)
        self.assertNotIn(".testTarget", manifest)
        self.assertNotIn("unsafeFlags", manifest)
        self.assertNotIn(str(self.root), manifest)
        self.assertNotIn("platforms/apple", manifest)
        self.assertNotIn("@UNIFFI_PACKAGE_PATH@", manifest)
        files = tree_bytes(self.output)
        self.assertEqual(
            set(files),
            {
                "Package.swift",
                ".blehub-apple-backend-stage.json",
                "Sources/BleHubApple/Backend.swift",
                "Sources/BleHubApple/Nested/Ownership.swift",
            },
        )
        self.assertEqual(
            files["Sources/BleHubApple/Backend.swift"],
            original_source["Backend.swift"],
        )
        self.assertEqual(tree_bytes(self.uniffi), original_input)
        self.assertEqual(tree_bytes(self.source), original_source)
        metadata = json.loads(files[".blehub-apple-backend-stage.json"])
        self.assertEqual(metadata["owner"], "tools.apple.stage_backend")
        self.assertEqual(metadata["uniffi_flavor"], "production")
        self.assertEqual(
            metadata["source_sha256"]["Backend.swift"],
            hashlib.sha256(original_source["Backend.swift"]).hexdigest(),
        )
        self.assertEqual(
            (self.output / metadata["uniffi_package"]).resolve(), self.uniffi
        )

    def test_owned_restage_removes_stale_files_and_can_select_bleng(self) -> None:
        stage_backend.stage(self.uniffi, self.output)
        (self.output / "stale.swift").write_text("// obsolete\n")
        (self.uniffi / "BleHubUniFfi.flavor").write_text("bleng\n")
        original_input = tree_bytes(self.uniffi)
        (self.source / "Backend.swift").write_text("// updated backend\n")
        stage_backend.stage(self.uniffi, self.output)
        self.assertFalse((self.output / "stale.swift").exists())
        self.assertEqual(
            (self.output / "Sources/BleHubApple/Backend.swift").read_text(),
            "// updated backend\n",
        )
        metadata = json.loads(
            (self.output / ".blehub-apple-backend-stage.json").read_text()
        )
        self.assertEqual(metadata["uniffi_flavor"], "bleng")
        self.assertEqual(tree_bytes(self.uniffi), original_input)

    def test_missing_or_invalid_inputs_preserve_owned_output(self) -> None:
        stage_backend.stage(self.uniffi, self.output)
        original = tree_bytes(self.output)
        required = [
            self.uniffi / "Package.swift",
            self.uniffi / "BleHubUniFfiNative.xcframework",
            self.uniffi / "BleHubUniFfi.flavor",
            self.source,
            self.template,
        ]
        for path in required:
            with self.subTest(missing=path.name):
                backup = path.with_name(path.name + ".hidden")
                path.rename(backup)
                try:
                    with self.assertRaisesRegex(
                        RuntimeError, "missing|source|template"
                    ):
                        stage_backend.stage(self.uniffi, self.output)
                    self.assertEqual(tree_bytes(self.output), original)
                finally:
                    backup.rename(path)
        (self.uniffi / "BleHubUniFfi.flavor").write_text("test-native-image\n")
        with self.assertRaisesRegex(RuntimeError, "flavor"):
            stage_backend.stage(self.uniffi, self.output)
        self.assertEqual(tree_bytes(self.output), original)
        (self.uniffi / "BleHubUniFfi.flavor").write_text("production\n")
        self.template.write_text("// no dependency placeholder\n")
        with self.assertRaisesRegex(RuntimeError, "template"):
            stage_backend.stage(self.uniffi, self.output)
        self.assertEqual(tree_bytes(self.output), original)

    def test_unsafe_outputs_never_change_inputs_or_user_directories(self) -> None:
        outside = self.root / "user directory"
        outside.mkdir()
        (outside / "keep.txt").write_text("user data\n")
        original = tree_bytes(self.root)
        for output in [
            outside,
            self.root / ".work/staged",
            self.root / ".work/staged/../outside",
            self.source,
            self.source.parent,
            self.uniffi,
            self.uniffi.parent,
            self.uniffi / "backend",
        ]:
            with self.subTest(output=output):
                with self.assertRaisesRegex(RuntimeError, "managed|overlap"):
                    stage_backend.stage(self.uniffi, output)
                self.assertEqual(tree_bytes(self.root), original)

    def test_refuse_unowned_output_and_symlink_output_or_parent(self) -> None:
        self.output.mkdir()
        (self.output / "keep.txt").write_text("not owned\n")
        marker = self.output / ".blehub-apple-backend-stage.json"
        for value in [None, "not JSON", '{"owner": "another.tool", "schema": 1}']:
            with self.subTest(marker=value):
                if value is not None:
                    marker.write_text(value)
                original = tree_bytes(self.output)
                with self.assertRaisesRegex(RuntimeError, "owned"):
                    stage_backend.stage(self.uniffi, self.output)
                self.assertEqual(tree_bytes(self.output), original)
        link = self.root / ".work/staged/linked"
        link.symlink_to(self.output, target_is_directory=True)
        for output in [link, link / "nested"]:
            with self.subTest(link=output):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    stage_backend.stage(self.uniffi, output)
                self.assertTrue(link.is_symlink())
                self.assertFalse((self.output / "nested").exists())

    def test_linked_source_and_copy_failure_preserve_previous_package(self) -> None:
        stage_backend.stage(self.uniffi, self.output)
        original = tree_bytes(self.output)
        linked = self.source / "Linked.swift"
        linked.symlink_to(self.source / "Backend.swift")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            stage_backend.stage(self.uniffi, self.output)
        self.assertEqual(tree_bytes(self.output), original)
        linked.unlink()
        with (
            patch.object(
                stage_backend.shutil, "copy2", side_effect=OSError("copy failed")
            ),
            self.assertRaisesRegex(OSError, "copy failed"),
        ):
            stage_backend.stage(self.uniffi, self.output)
        self.assertEqual(tree_bytes(self.output), original)
        self.assertEqual(
            {path.name for path in self.output.parent.iterdir()},
            {"backend output", "input artifacts"},
        )


if __name__ == "__main__":
    unittest.main()
