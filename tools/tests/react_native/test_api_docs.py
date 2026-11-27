"""The package's API reference must name every capability a user can call, in both languages.

A reference that drifts from the specs is worse than none: it is the first thing a new consumer
reads. The capability set is derived from the Nitro specs themselves (every interface that declares
a method), so adding a capability without documenting it fails here.

The reference ships as a mirror pair — `API.zh.md` (中文) and `API.md` (English). Both are checked
against the same derived set, because a translation that silently drops a capability is exactly the
kind of drift a second copy invites.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "integrations" / "react-native-bleng"
SPEC_ROOT = PACKAGE / "specs"
# Every language the reference ships in. A new one joins this mapping, not a new test.
REFERENCES = {
    "zh": PACKAGE / "API.zh.md",
    "en": PACKAGE / "API.md",
}
# The ownership README ships in the same two languages and must stay mutually reachable.
READMES = {
    "zh": PACKAGE / "README.zh-CN.md",
    "en": PACKAGE / "README.md",
}
# The exhaustive type inventory is generated from the specs, so it cannot rot; the reference
# is prose. A public name must appear in at least one of them.
INVENTORY = ROOT / "docs" / "research" / "bleng" / "abi-freeze-inventory.md"
INDEX = PACKAGE / "src" / "index.ts"

# The derived-layer claim, stated in each language's own words. Keep these aligned with the text:
# if a translation stops saying "no new semantics" or "dropping never cancels", the guard is gone.
DERIVED_CLAIMS = {
    "zh": ("不新增任何语义", "丢弃 `result` 不会取消"),
    "en": ("adds **no** new semantics", "Dropping `result` never cancels"),
}
HELPERS = (
    "pendingOperation",
    "requireAccepted",
    "BlengOperationError",
    "isBlengFailure",
)

_INTERFACE = re.compile(r"^export interface (\w+)")
_METHOD = re.compile(r"^  \w+\(")
_EXPORT_BLOCK = re.compile(r"export (?:type )?\{([^}]*)\}", re.DOTALL)


def capabilities() -> list[str]:
    """Interfaces that declare at least one method: the things a caller invokes."""
    names: list[str] = []
    for path in sorted(SPEC_ROOT.glob("*.nitro.ts")):
        current: str | None = None
        has_method = False
        for line in path.read_text(encoding="utf-8").splitlines():
            found = _INTERFACE.match(line)
            if found is not None:
                if current is not None and has_method:
                    names.append(current)
                current, has_method = found.group(1), False
                continue
            if _METHOD.match(line):
                has_method = True
        if current is not None and has_method:
            names.append(current)
    return names


def exported_values() -> list[str]:
    """Every name `src/index.ts` re-exports, value or type."""
    text = INDEX.read_text(encoding="utf-8")
    names: list[str] = []
    for block in _EXPORT_BLOCK.findall(text):
        for item in block.replace("\n", " ").split(","):
            name = item.strip().split(" as ")[-1].strip()
            if name:
                names.append(name)
    return names


class ApiDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.references = {
            language: path.read_text(encoding="utf-8")
            for language, path in REFERENCES.items()
        }
        self.inventory = INVENTORY.read_text(encoding="utf-8")

    def test_every_callable_capability_is_documented_in_every_language(self) -> None:
        for language, text in self.references.items():
            missing = [name for name in capabilities() if name not in text]
            self.assertEqual(
                missing, [], f"{language}: undocumented capabilities: {missing}"
            )

    def test_the_await_layer_is_documented_as_derived_in_every_language(self) -> None:
        # The layer must never read as a second operation model: each reference has to say that it
        # settles through the operation's own wait and that dropping a promise does not cancel.
        for language, text in self.references.items():
            for phrase in HELPERS:
                self.assertIn(phrase, text, f"{language}: {phrase} is not documented")
            for phrase in DERIVED_CLAIMS[language]:
                self.assertIn(
                    phrase, text, f"{language}: derived claim missing ({phrase})"
                )

    def test_every_readme_links_its_mirror(self) -> None:
        for language, path in READMES.items():
            mirrors = [other.name for other in READMES.values() if other != path]
            self.assertTrue(
                any(mirror in path.read_text(encoding="utf-8") for mirror in mirrors),
                f"{language}: README does not link its mirror ({mirrors})",
            )

    def test_every_reference_links_its_mirror(self) -> None:
        # A mirror nobody can find is not a mirror.
        for language, path in REFERENCES.items():
            mirrors = [other.name for other in REFERENCES.values() if other != path]
            self.assertTrue(
                any(mirror in self.references[language] for mirror in mirrors),
                f"{language}: reference does not link its mirror ({mirrors})",
            )

    def test_every_public_entry_point_is_named_in_the_reference_or_the_inventory(
        self,
    ) -> None:
        known = "".join(self.references.values()) + self.inventory
        missing = [name for name in exported_values() if name not in known]
        self.assertEqual(missing, [], f"exports missing from both documents: {missing}")

    def test_every_example_is_reachable_from_the_package_readme(self) -> None:
        # An example nobody links is dead weight: a new consumer starts at the package README, so
        # each live example directory must be named there and explain itself in its own README.
        # `examples/archive` is history by convention (like `docs/research/archive`), so it is out
        # of scope: those lanes were retired, not offered.
        readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
        examples = ROOT / "examples"
        unreachable = [
            path.name
            for path in sorted(examples.iterdir())
            if path.is_dir()
            and path.name not in {"archive"}
            and not path.name.startswith(".")
            and (not (path / "README.md").is_file() or path.name not in readme)
        ]
        self.assertEqual(
            unreachable,
            [],
            f"examples not linked from the package README: {unreachable}",
        )


if __name__ == "__main__":
    unittest.main()
