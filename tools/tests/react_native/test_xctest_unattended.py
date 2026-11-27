"""Hardware-free regressions for the read-only XCTest host-switch report."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from tools.react_native import xctest_unattended


class XctestUnattendedTests(unittest.TestCase):
    def dev_tools(self, stdout: str, *, returncode: int = 0):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def test_developer_mode_is_read_from_the_tool_not_assumed(self) -> None:
        with patch.object(
            xctest_unattended.shutil, "which", return_value="/usr/bin/DevToolsSecurity"
        ):
            with patch.object(
                xctest_unattended.subprocess,
                "run",
                return_value=self.dev_tools("Developer mode is currently enabled."),
            ):
                self.assertEqual(xctest_unattended.developer_mode_state(), "unattended")
            with patch.object(
                xctest_unattended.subprocess,
                "run",
                return_value=self.dev_tools("Developer mode is currently disabled."),
            ):
                self.assertEqual(
                    xctest_unattended.developer_mode_state(), "requires-authentication"
                )
            with patch.object(
                xctest_unattended.subprocess,
                "run",
                return_value=self.dev_tools("Something else entirely."),
            ):
                self.assertEqual(xctest_unattended.developer_mode_state(), "unknown")
            with patch.object(
                xctest_unattended.subprocess,
                "run",
                return_value=self.dev_tools(
                    "Developer mode is currently enabled.", returncode=1
                ),
            ):
                self.assertEqual(xctest_unattended.developer_mode_state(), "unknown")

    def test_automation_authentication_policy_is_independent_of_session_state(
        self,
    ) -> None:
        cases = (
            ("disabled", "does not require", "unattended"),
            ("enabled", "requires", "requires-authentication"),
        )
        with (
            patch.object(xctest_unattended, "_marker_state", return_value="unknown"),
            patch.object(
                xctest_unattended.shutil, "which", return_value="automationmodetool"
            ),
            patch.object(xctest_unattended.subprocess, "run") as run,
        ):
            for session, policy, expected in cases:
                with self.subTest(session=session, policy=policy):
                    run.return_value = self.dev_tools(
                        f"Automation Mode is {session}.\n"
                        f"This device {policy} user authentication to enable Automation Mode."
                    )
                    self.assertEqual(
                        xctest_unattended.automation_mode_authentication(), expected
                    )

    def test_a_missing_tool_is_unknown_and_never_a_failure(self) -> None:
        with patch.object(xctest_unattended.shutil, "which", return_value=None):
            self.assertEqual(xctest_unattended.developer_mode_state(), "unknown")

    def test_a_locked_keychain_is_reported_not_raised(self) -> None:
        with patch.object(
            xctest_unattended,
            "require_unlocked_keychain",
            side_effect=xctest_unattended.UnattendedError("locked"),
        ):
            self.assertEqual(
                xctest_unattended.keychain_lock_state(), "requires-authentication"
            )
        with patch.object(
            xctest_unattended, "require_unlocked_keychain", return_value="unlocked"
        ):
            self.assertEqual(xctest_unattended.keychain_lock_state(), "unattended")
        with patch.object(
            xctest_unattended, "require_unlocked_keychain", return_value="unknown"
        ):
            self.assertEqual(xctest_unattended.keychain_lock_state(), "unknown")

    def test_the_report_answers_every_switch_and_only_blocks_on_evidence(self) -> None:
        states = {
            "automation_mode": "unattended",
            "developer_mode": "requires-authentication",
            "keychain": "unattended",
        }
        with (
            patch.object(
                module := xctest_unattended,
                "automation_mode_authentication",
                side_effect=lambda: states["automation_mode"],
            ),
            patch.object(
                module,
                "developer_mode_state",
                side_effect=lambda: states["developer_mode"],
            ),
            patch.object(
                module,
                "keychain_lock_state",
                side_effect=lambda: states["keychain"],
            ),
        ):
            self.assertEqual(set(module.report()), set(states))
            self.assertFalse(module.unattended())
            # `unknown` is not evidence of a missing switch.
            states["developer_mode"] = "unknown"
            self.assertTrue(module.unattended())
            states["automation_mode"] = "requires-authentication"
            self.assertFalse(module.unattended())

    def test_the_cli_names_the_remedy_for_a_switch_that_is_off(self) -> None:
        with (
            patch.object(
                xctest_unattended,
                "automation_mode_authentication",
                return_value="requires-authentication",
            ),
            patch.object(
                xctest_unattended, "developer_mode_state", return_value="unattended"
            ),
            patch.object(
                xctest_unattended, "keychain_lock_state", return_value="unknown"
            ),
        ):
            printed = io.StringIO()
            with redirect_stdout(printed):
                self.assertEqual(xctest_unattended.main(["--json"]), 0)
            document = json.loads(printed.getvalue())
            self.assertFalse(document["unattended"])
            self.assertEqual(
                document["switches"]["automation_mode"], "requires-authentication"
            )

            printed = io.StringIO()
            with redirect_stdout(printed):
                xctest_unattended.main([])
            text = printed.getvalue()
            self.assertIn("xctest_automation_mode=requires-authentication", text)
            self.assertIn("xctest_keychain=unknown", text.splitlines())
            self.assertIn("enable-automation-mode", text)
            device_hint = next(
                line
                for line in text.splitlines()
                if line.startswith("xctest_documented_device_half=")
            )
            self.assertIn("iPhone Settings > Developer > UI Automation", device_hint)
            self.assertIn("operator", device_hint)
            self.assertNotIn("accept", device_hint.lower())
            self.assertIn("xctest_host_unattended=no", text)


if __name__ == "__main__":
    unittest.main()
