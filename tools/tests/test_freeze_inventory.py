"""The freeze inventory is a regenerated document, so it cannot drift from its sources.

A human-readable freeze list that quietly stops matching the reviewed snapshots is worse than no
list at all: it is a signature over stale shapes. This test regenerates it and compares.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools import freeze_inventory

ROOT = Path(__file__).resolve().parents[2]


class FreezeInventoryTests(unittest.TestCase):
    def test_the_document_on_disk_is_the_one_this_repository_renders(self) -> None:
        published = ROOT / "docs" / "research" / "bleng" / "abi-freeze-inventory.md"
        self.assertTrue(published.is_file(), published)
        self.assertEqual(
            published.read_text(encoding="utf-8"),
            freeze_inventory.render(),
            "regenerate with `pixi run just binding freeze-inventory`",
        )

    def test_every_component_has_all_three_reviewed_snapshots(self) -> None:
        for component, _ in freeze_inventory.COMPONENTS:
            for language in freeze_inventory.LANGUAGES:
                path = freeze_inventory.snapshot_path(component, language)
                with self.subTest(component=component, language=language):
                    self.assertTrue(path.is_file(), path)
                    self.assertTrue(
                        freeze_inventory.declarations(path),
                        f"{path.name} declares nothing",
                    )

    def test_the_js_surface_is_not_empty(self) -> None:
        specs = freeze_inventory.nitro_surface()
        # Named count on purpose: a new spec file is a decision, not a discovery.
        self.assertEqual(len(specs), 9, [name for name, _, _ in specs])
        declared = sum(len(interfaces) for _, interfaces, _ in specs)
        self.assertGreater(declared, 10, "the Nitro surface rendered almost nothing")

    def test_the_code_space_is_grouped_by_the_three_wire_shapes(self) -> None:
        shapes = {
            shape: literals
            for shape, _, literals in freeze_inventory.failure_code_groups()
        }
        self.assertEqual(
            set(shapes), {"bleng.<code>", "<path>.<code>", "原样"}, sorted(shapes)
        )
        # Every group must be non-empty, or a whole class of wire codes would be missing from
        # the list a human signs.
        for shape, literals in shapes.items():
            self.assertTrue(literals, shape)
        # A prefix is not a code: the paths belong to the delegated rule, not to `bleng.<code>`.
        for path in freeze_inventory.FAILURE_PATHS:
            self.assertNotIn(path, shapes["bleng.<code>"])
        for code in shapes["原样"]:
            self.assertTrue(code.startswith("supervisor."), code)


if __name__ == "__main__":
    unittest.main()
