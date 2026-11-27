"""The device-observed population keeps its shape, or it stops meaning anything.

`tools/tests/observed` holds the tests whose input is what a real device once answered. That
is worth something only while a reader can trust three things at a glance: every test in here
is named `test_regression_*`, no such test lives anywhere else, and each one says what was
seen, where and when.

The name carries the executable part of the contract. Both `unittest` and `pytest` collect
`test*` and nothing else, so an observed test named `regression_*` alone would pass every
review and never run; that happened here once, to five tests.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBSERVED = ROOT / "observed"
MARKER = "regression_"
REQUIRED = "test_" + MARKER
# Each observed test states these, in the comment block directly above it.
FACTS = ("Trigger:", "Symptom:", "Provenance:")
DATE = re.compile(r"\b20[0-9]{2}-[01][0-9]-[0-3][0-9]\b")
THIS_FILE = Path(__file__).resolve()


def modules() -> list[Path]:
    """Every test module under the discovery root, the observed population included."""
    return sorted(
        path
        for path in ROOT.rglob("test_*.py")
        if "support" not in path.parts and path.resolve() != THIS_FILE
    )


def functions_in(path: Path) -> list[tuple[ast.FunctionDef, int, int]]:
    """Each candidate test with the line range of the comment block that precedes it.

    Module-level functions count: a regression outside a class is still part of the
    population, and skipping it would let it bypass every rule below.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes: list[ast.Module | ast.ClassDef] = [tree]
    scopes.extend(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    found: list[tuple[ast.FunctionDef, int, int]] = []
    for scope in scopes:
        # A class's own line, or the end of the module docstring, is where the first
        # function's preceding comment block stops.
        previous_end = 1
        if isinstance(scope, ast.ClassDef):
            previous_end = scope.lineno
        elif _is_docstring(scope.body[:1]):
            previous_end = scope.body[0].end_lineno
        for item in scope.body:
            if isinstance(item, ast.FunctionDef) and item.name.startswith(
                ("test_", MARKER)
            ):
                # One-based `end` is the def's own line; the comment stops before it.
                found.append((item, previous_end, item.lineno - 1))
            previous_end = item.end_lineno
    return found


def _is_docstring(head: list[ast.stmt]) -> bool:
    """True when these are a module's leading docstring statement."""
    return (
        len(head) == 1
        and isinstance(head[0], ast.Expr)
        and isinstance(head[0].value, ast.Constant)
        and isinstance(head[0].value.value, str)
    )


def observed_modules() -> list[Path]:
    """The observed modules, excluding this guard."""
    return [path for path in modules() if OBSERVED in path.parents]


class ObservedPopulationTests(unittest.TestCase):
    def test_the_population_is_non_empty_and_every_test_in_it_is_a_regression(
        self,
    ) -> None:
        observed = observed_modules()
        self.assertTrue(observed, "no device-observed regression has been recorded")
        for path in observed:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(path.parent, OBSERVED, "the population is not nested")
                self.assertTrue(path.name.startswith("test_"))
                functions = functions_in(path)
                self.assertTrue(functions, "an observed module with no test")
                for function, _, _ in functions:
                    self.assertTrue(function.name.startswith(REQUIRED), function.name)

    def test_no_regression_is_recorded_anywhere_else(self) -> None:
        # One home: a regression outside this directory would not be covered by the rules
        # below, and the `-k regression_` slice would silently widen.
        for path in modules():
            if OBSERVED in path.parents:
                continue
            for function, _, _ in functions_in(path):
                with self.subTest(path=path.relative_to(ROOT), test=function.name):
                    self.assertNotIn(MARKER, function.name)

    def test_every_regression_states_what_was_seen_where_and_when(self) -> None:
        for path in observed_modules():
            lines = path.read_text(encoding="utf-8").split("\n")
            for function, start, end in functions_in(path):
                comment = "\n".join(lines[start:end])
                with self.subTest(path=path.relative_to(ROOT), test=function.name):
                    for fact in FACTS:
                        self.assertIn(fact, comment)
                    provenance = comment[comment.index("Provenance:") :]
                    self.assertIsNotNone(
                        DATE.search(provenance),
                        "the provenance names the day the device answered this",
                    )

    def test_each_module_says_what_its_population_is(self) -> None:
        for path in observed_modules():
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(
                    (ast.get_docstring(ast.parse(path.read_text())) or "").startswith(
                        "Device-observed regressions"
                    )
                )


if __name__ == "__main__":
    unittest.main()
