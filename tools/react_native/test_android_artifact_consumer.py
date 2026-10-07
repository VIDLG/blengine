"""Host-only boundary tests for the clean Android RN artifact consumer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import android_artifact_consumer as consumer


class AndroidArtifactConsumerTests(unittest.TestCase):
    def test_stage_package_excludes_android_build_outputs_and_ios_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration = root / "integration"
            for relative in consumer.INTEGRATION_FILES:
                path = integration / relative
                if path.suffix:
                    _ = path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_text("fixture\n", encoding="utf-8")
                else:
                    _ = path.mkdir(parents=True, exist_ok=True)
            _ = (integration / "package.json").write_text("{}\n", encoding="utf-8")
            _ = (integration / "nitrogen/generated/android/kotlin").mkdir(parents=True)
            _ = (integration / "nitrogen/generated/shared/c++").mkdir(parents=True)
            _ = (integration / "android/src/main/Hybrid.kt").write_text(
                "package fixture\n", encoding="utf-8"
            )
            _ = (integration / "android/src/main/.cxx/debug/metadata").mkdir(
                parents=True
            )
            _ = (integration / "android/src/main/.cxx/debug/metadata/stale").write_text(
                "must not stage\n", encoding="utf-8"
            )
            _ = (integration / "android/src/main/build/generated").mkdir(parents=True)
            _ = (integration / "android/src/main/build/generated/stale").write_text(
                "must not stage\n", encoding="utf-8"
            )
            _ = (integration / "ios/secret.swift").parent.mkdir(parents=True)
            _ = (integration / "ios/secret.swift").write_text(
                "must not stage\n", encoding="utf-8"
            )

            layout = consumer.make_layout(root / "work")
            _ = layout.package_root.mkdir(parents=True)
            with patch.object(consumer, "INTEGRATION_ROOT", integration):
                consumer.stage_package(layout)

            self.assertTrue(
                (layout.package_root / "android/src/main/Hybrid.kt").is_file()
            )
            self.assertFalse((layout.package_root / "android/src/main/.cxx").exists())
            self.assertFalse((layout.package_root / "android/src/main/build").exists())
            self.assertFalse((layout.package_root / "ios").exists())


if __name__ == "__main__":
    _ = unittest.main()
