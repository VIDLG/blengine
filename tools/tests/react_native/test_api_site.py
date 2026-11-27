"""The generated TypeDoc site must stay derived: one entry point, and a landing page that points home.

The site is deliberately never edited by hand — it exists so a reader can see exact signatures
without trusting prose. Two facts keep that honest and are cheap to check without running Node here:

- the entry point is the package's public entry point, so pages follow `src/index.ts` exports instead
  of a hand-kept list that could quietly stop covering a new capability;
- the landing page names both hand-written references, so a reader who lands on the site first cannot
  mistake a declaration for the behavioural contract.

Everything else about the site is TypeScript's job; `pixi run just react-native::api-site-check`
runs the real generator with `--treatWarningsAsErrors`.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "integrations" / "react-native-bleng"
CONFIG = PACKAGE / "typedoc.json"


class ApiSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_entry_point_is_the_public_entry(self) -> None:
        self.assertEqual(self.config["entryPoints"], ["src/index.ts"])
        self.assertTrue((PACKAGE / "src" / "index.ts").is_file())
        self.assertEqual(self.config["tsconfig"], "tsconfig.json")
        self.assertTrue((PACKAGE / "tsconfig.json").is_file())

    def test_output_and_readme_stay_inside_the_package(self) -> None:
        out = Path(self.config["out"])
        readme = Path(self.config["readme"])
        self.assertFalse(
            out.is_absolute(), "a generated site path must be package-relative"
        )
        self.assertFalse(
            readme.is_absolute(), "the landing page must live with the package"
        )
        self.assertTrue((PACKAGE / readme).is_file())

    def test_landing_page_points_at_both_references(self) -> None:
        text = (PACKAGE / self.config["readme"]).read_text(encoding="utf-8")
        for reference in ("API.md", "API.zh.md"):
            self.assertIn(reference, text, f"the site's landing page omits {reference}")


if __name__ == "__main__":
    unittest.main()
