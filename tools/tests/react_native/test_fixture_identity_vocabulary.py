"""One vocabulary for the fixture identity on every Android lane's command line.

A runner's command line is a published surface: it appears in reports, in the just recipes and in
whatever someone copies out of a terminal. The identity is the *canonical resource* of a physically
attached probe (`jlink:<serial>`), so it has one spelling and one declaration. Two runners once
spelled it `--fixture-identity`, and a run failed on the flag name rather than on anything about the
hardware.

The check reads each runner's own parser. Every Android lane exposes `build_parser()`, so the CLI is
a value that can be inspected without hardware — which is why the shared groups live in
`android_lane_cli.py` rather than being retyped per lane.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.react_native import (
    run_bleng_android_adapter_transition,
    run_bleng_android_multi_link,
    run_bleng_android_native_notification,
    run_bleng_android_permission_transition,
    run_bleng_android_scan,
)

RUNNER_ROOT = Path(__file__).resolve().parents[3] / "tools" / "react_native"

IDENTITY = "--fixture-resource-identity"
PORT = "--fixture-port"
SHORT = "--fixture-identity"

# Named, so adding a sixth lane is an explicit decision here rather than a silently-accepted N+1.
SINGLE_FIXTURE = {
    "run_bleng_android_adapter_transition": run_bleng_android_adapter_transition,
    "run_bleng_android_native_notification": run_bleng_android_native_notification,
    "run_bleng_android_permission_transition": run_bleng_android_permission_transition,
    "run_bleng_android_scan": run_bleng_android_scan,
}
# The peer list *is* the topology: each declared peer carries its own port, contract, profile and
# identity, so this lane names no single-fixture flag.
MULTI_FIXTURE = {
    "run_bleng_android_multi_link": run_bleng_android_multi_link,
}


def options(module: object) -> set[str]:
    """Every option string a runner accepts, read from the parser it publishes."""
    parser = module.build_parser()
    return {option for action in parser._actions for option in action.option_strings}


class FixtureIdentityVocabularyTests(unittest.TestCase):
    def test_every_single_fixture_lane_names_the_identity_canonically(self) -> None:
        for name, module in SINGLE_FIXTURE.items():
            declared = options(module)
            self.assertIn(PORT, declared, f"{name} does not declare the fixture port")
            self.assertIn(
                IDENTITY,
                declared,
                f"{name} does not name the fixture identity canonically",
            )

    def test_the_multi_fixture_lane_declares_identity_per_peer(self) -> None:
        declared = options(MULTI_FIXTURE["run_bleng_android_multi_link"])
        self.assertIn("--peer", declared)
        self.assertNotIn(PORT, declared, "a peer declaration carries its own port")

    def test_no_runner_keeps_the_short_spelling(self) -> None:
        offenders = [
            path.name
            for path in sorted(RUNNER_ROOT.glob("run_bleng_*.py"))
            if SHORT in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(
            offenders, [], "fixture identity has one spelling; these disagree"
        )

    def test_the_shared_groups_declare_each_flag_once(self) -> None:
        # The point of `android_lane_cli`: a group is written once, so a new fixture flag cannot
        # exist on five lanes and be missing on the sixth.
        native = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(RUNNER_ROOT.glob("run_bleng_android_*.py"))
            if path.name != "run_bleng_android_scan.py"
        }
        sources = {
            **native,
            "android_lane_cli.py": (RUNNER_ROOT / "android_lane_cli.py").read_text(
                encoding="utf-8"
            ),
        }
        for flag in ("--fixture-port", IDENTITY):
            declaring = sorted(
                name for name, text in sources.items() if f'"{flag}"' in text
            )
            self.assertEqual(
                declaring,
                ["android_lane_cli.py"],
                f"{flag} must be declared once, in the shared group: {declaring}",
            )

    def test_the_managed_scene_lane_shares_the_fixture_group(self) -> None:
        # The managed UI-scene family states wider authorizations (it installs the workbench App
        # itself), so it keeps its own authorization wording — but the fixture it leases is the
        # same kind of thing, so it never retypes that group.
        text = (RUNNER_ROOT / "run_bleng_android_scan.py").read_text(encoding="utf-8")
        self.assertIn("add_fixture(parser)", text)
        self.assertNotIn(f'"{PORT}"', text)
        self.assertNotIn(f'"{IDENTITY}"', text)
