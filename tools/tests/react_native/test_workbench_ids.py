"""One accessibility-identifier vocabulary, enforced from both sides.

The App's ``testID`` strings are the only contract between three independent drivers
(our XCTest scene target, Android instrumented tests, and the host-side scene code).
A typo used to surface minutes into a real-device run; these checks make it a CI
failure instead.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tests/consumers/react-native-bleng/src"
IOS_ROOT = ROOT / "tests/consumers/react-native-bleng/ios"
IDS = SRC / "ids.ts"
HOST = ROOT / "tools/react_native"

QUOTED = re.compile(r"'([^'\n]+)'")
KEBAB = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")
HOST_USE = re.compile(
    r"\.(?:click|click_settled|visible|text|_reveal|_observe)\(\s*[\"']([a-z0-9][a-z0-9-]+)[\"']"
)
NATIVE_VIEW = re.compile(r"requireNativeComponent|codegenNativeComponent")


def app_ids() -> list[str]:
    values: list[str] = []
    for match in QUOTED.finditer(IDS.read_text(encoding="utf-8")):
        candidate = match.group(1)
        if KEBAB.match(candidate):
            values.append(candidate)
    return values


def host_ids() -> set[str]:
    found: set[str] = set()
    for path in sorted(HOST.glob("*.py")):
        found |= set(HOST_USE.findall(path.read_text(encoding="utf-8")))
    return found


def plan_ids() -> set[str]:
    """Identifiers the declarative scene plans drive.

    A plan reaches a device through the plan itself, not through a `.click("id")` call,
    so it needs the same guard: an identifier that is not in the App's vocabulary must
    fail here instead of on a phone two minutes into a scene. The plans are imported
    lazily because they are the lane's data, not this test's dependency graph.
    """
    from tools.react_native.run_ios_scene_driver import PLANS

    found: set[str] = set()
    for steps in PLANS.values():
        for step in steps:
            for source in (step, step.get("when"), step.get("until")):
                if isinstance(source, dict) and isinstance(source.get("id"), str):
                    found.add(source["id"])
    return found


def scrollable_content_ids() -> set[str]:
    """Identifiers rendered only inside the scroll container, derived from the source.

    `App.tsx` keeps the machine surface pinned and renders `ManualScan`'s panels inside
    `workbench-content`; the panels are the human view. A native plan must not drive any
    of them, because the in-process driver cannot scroll and Android omits off-viewport
    children entirely. Groups are read from `ids.ts` so the mapping is by declaration
    rather than by guessing a leaf name.
    """
    groups: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in IDS.read_text(encoding="utf-8").splitlines():
        group = re.fullmatch(r"  (\w+): frozen\(\{", line)
        if group:
            current = group.group(1)
            groups[current] = {}
            continue
        if current is None:
            continue
        if line.strip() == "}),":
            current = None
            continue
        entry = re.fullmatch(r"    (\w+): '([^']+)',", line)
        if entry:
            groups[current][entry.group(1)] = entry.group(2)
    content = (SRC / "ManualScan.tsx").read_text(encoding="utf-8")
    found: set[str] = set()
    for path in re.findall(r"Ids\.(\w+)\.(\w+)(?!\s*\()", content):
        for group, name in [path]:
            literal = groups.get(group, {}).get(name)
            if literal is not None:
                found.add(literal)
    # `App.tsx` renders inline children inside the same `workbench-content` ScrollView
    # (Evidence text, and formerly the Tests-page Start button). Parsing only
    # `ManualScan.tsx` let a plan drive a node that was technically inside the scroll
    # container; the block is scanned here so a new inline node cannot reintroduce that.
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    opening = app.find("<ScrollView testID={Ids.workbench.content}")
    if opening < 0:
        raise AssertionError(
            "App.tsx no longer renders the workbench-content ScrollView"
        )
    closing = app.find("</ScrollView>", opening)
    if closing < 0:
        raise AssertionError("the workbench-content ScrollView is never closed")
    for group, name in re.findall(r"Ids\.(\w+)\.(\w+)(?!\s*\()", app[opening:closing]):
        literal = groups.get(group, {}).get(name)
        if literal is not None:
            found.add(literal)
    return found


class WorkbenchIdTests(unittest.TestCase):
    def test_the_vocabulary_has_no_duplicate_or_foreign_identifiers(self) -> None:
        values = app_ids()
        duplicates = sorted({value for value in values if values.count(value) > 1})
        self.assertEqual(duplicates, [], "two vocabulary entries share one identifier")
        # Every identifier must be kebab-case: RN maps it to an accessibility id on
        # iOS and a test tag on Android, and mixed case drifts between drivers.
        for value in values:
            self.assertRegex(value, KEBAB, f"non-kebab identifier: {value!r}")
            self.assertLessEqual(len(value), 64, value)

    def test_no_component_owns_a_literal_test_id(self) -> None:
        for path in sorted(SRC.glob("*.tsx")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                'testID="',
                text,
                f"{path.name} must use Ids.* instead of a literal testID",
            )
        components = "".join(
            path.read_text(encoding="utf-8") for path in sorted(SRC.glob("*.tsx"))
        )
        self.assertIn("Ids.", components, "components must reference the vocabulary")

    def test_every_host_required_identifier_exists_in_the_app(self) -> None:
        known = set(app_ids())
        # The nav helper composes `workbench-<page>`; the pages are the vocabulary's
        # own nav keys, so they are covered by construction rather than by a literal.
        required = host_ids() | plan_ids()
        missing = sorted(required - known)
        self.assertEqual(
            missing,
            [],
            "host scene code drives identifiers the App does not declare: "
            f"{missing}; add them to ids.ts (or fix the host typo)",
        )

    def test_the_title_is_the_keyboard_dismiss_surface(self) -> None:
        """The title is the one element a driver taps that no plan names.

        A scene sets text fields, and the software keyboard they raise covers the bottom of
        the screen where pinned controls live: `pull-notification` at y≈622 under a keyboard
        that starts at y≈578 is present, enabled and unreachable (measured on `disconnect`,
        2026-11-26). The App therefore gives the driver one tap-to-dismiss surface, and the
        driver names it by the same identifier.
        """
        app = (SRC / "App.tsx").read_text(encoding="utf-8")
        title = re.search(
            r"<Text testID=\{Ids\.workbench\.title\}[^>]*onPress=\{([^}]*)\}", app
        )
        self.assertIsNotNone(
            title,
            "the workbench title lost its onPress: the scene drivers have no way to retire "
            "the software keyboard after setText",
        )
        self.assertIn("Keyboard.dismiss()", title.group(1))
        swift = (
            IOS_ROOT / "BleHubBlengHostUITests" / "BlengSceneDriverUITests.swift"
        ).read_text(encoding="utf-8")
        self.assertIn('identifier: "formal-host-title"', swift)
        self.assertIn("dismissKeyboard", swift)

    def test_a_native_plan_never_reaches_into_scrollable_content(self) -> None:
        """The in-process driver cannot scroll, and content is not machine surface."""
        overlap = sorted(plan_ids() & scrollable_content_ids())
        self.assertEqual(
            overlap,
            [],
            "a native plan drives scrollable content: pin that fact in the App instead "
            f"of scrolling for it: {overlap}",
        )

    def test_native_views_must_publish_the_same_identifier(self) -> None:
        """A hybrid/native view bypasses ``testID``; it needs its own mapping.

        Today the host App renders no native view, so the correct state is "none".
        The day one is added this test fails on purpose: set
        ``accessibilityIdentifier`` (iOS) and ``contentDescription`` + the view tag
        (Android) from the same ``Ids`` value, then narrow this assertion.
        """
        offenders = [
            path.name
            for path in sorted(SRC.glob("*.tsx"))
            if NATIVE_VIEW.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            offenders,
            [],
            "native view introduced: it must publish accessibilityIdentifier/"
            "contentDescription from Ids, then update this guard",
        )


if __name__ == "__main__":
    unittest.main()
