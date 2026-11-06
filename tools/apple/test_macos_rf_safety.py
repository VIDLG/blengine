"""Hardware-free admission regressions for the blocked native macOS RF gates."""

from __future__ import annotations

import contextlib
import io
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tools.apple import test_macos_runtime as macos
from tools.apple.rf_fixture import load_profile

ROOT = Path(__file__).resolve().parents[2]
V2_PROFILE = (
    ROOT / "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json"
)
HISTORICAL_PROFILE = ROOT / "tests/conformance/scenarios/blehub-nrf-indication-v2.json"
SHORTCUTS = {
    "rf-macos-nrf52840": "io",
    "rf-macos-nrf52840-notification": "notification",
    "rf-macos-nrf52840-indication": "indication",
    "rf-macos-nrf52840-rssi": "rssi",
    "rf-macos-nrf52840-passive-disconnect": "passive-disconnect",
}


class MacosRfAdmissionTests(unittest.TestCase):
    def test_cli_rejects_rf_before_any_external_action_or_owner_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "another-owner-coordination"
            coordination.mkdir()
            # Existing owner identity and primary/cleanup evidence must survive rejection.
            for name in ("fixture.pid", "failure.json", "cleanup-error.txt"):
                (coordination / name).write_text(name, encoding="utf-8")
            before = {p.name: p.read_bytes() for p in coordination.iterdir()}
            cases = [["rf-android"], ["rf-external", "io"]]
            for profile in (HISTORICAL_PROFILE, V2_PROFILE):
                for scenario in macos.EXTERNAL_MACOS_SCENARIOS:
                    cases.append(
                        [
                            "rf-external",
                            scenario,
                            "--profile",
                            str(profile),
                            "--coordination-path",
                            str(coordination),
                        ]
                    )
            # Even a caller-supplied request cannot unlock the unfinished receipt bridge.
            cases.append(
                [
                    "rf-external",
                    "io",
                    "--profile",
                    str(V2_PROFILE),
                    "--fixture-request",
                    str(root / "fixture-request.json"),
                ]
            )
            for arguments in cases:
                with self.subTest(arguments=arguments), ExitStack() as stack:
                    stack.enter_context(patch.object(macos.sys, "platform", "darwin"))
                    stack.enter_context(
                        patch.object(macos.sys, "argv", ["macos-probe", *arguments])
                    )
                    forbidden = [
                        stack.enter_context(patch.object(macos, name))
                        for name in (
                            "_required_executable",
                            "_build_bundle",
                            "fixture_from_arguments",
                            "_launch",
                            "_run_rf_scenario",
                        )
                    ]
                    for name in ("run", "Popen"):
                        forbidden.append(
                            stack.enter_context(patch.object(macos.subprocess, name))
                        )
                    forbidden.append(
                        stack.enter_context(patch.object(macos.os, "kill"))
                    )
                    forbidden.append(
                        stack.enter_context(patch.object(macos.shutil, "rmtree"))
                    )
                    stderr = stack.enter_context(
                        contextlib.redirect_stderr(io.StringIO())
                    )
                    with self.assertRaises(SystemExit) as caught:
                        macos.main()
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn("macOS RF blocked", stderr.getvalue())
                    self.assertIn("--fixture-request", stderr.getvalue())
                    self.assertIn("contract/profile", stderr.getvalue())
                    self.assertIn("live receipt", stderr.getvalue())
                    for action in forbidden:
                        action.assert_not_called()
                    self.assertEqual(
                        {p.name: p.read_bytes() for p in coordination.iterdir()}, before
                    )

    def test_admission_error_is_preserved_without_entering_unowned_cleanup(
        self,
    ) -> None:
        primary = RuntimeError("macOS RF blocked: primary session admission failure")
        with ExitStack() as stack:
            stack.enter_context(patch.object(macos.sys, "platform", "darwin"))
            stack.enter_context(
                patch.object(macos.sys, "argv", ["macos-probe", "rf-external", "io"])
            )
            stack.enter_context(
                patch.object(macos, "_require_macos_rf_session", side_effect=primary)
            )
            cleanup = stack.enter_context(
                patch.object(
                    macos,
                    "_terminate_probe_app",
                    side_effect=RuntimeError("foreign owner cleanup failure"),
                )
            )
            stop = stack.enter_context(
                patch.object(
                    macos.subprocess,
                    "run",
                    side_effect=RuntimeError("global stop failure"),
                )
            )
            stderr = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            with self.assertRaises(SystemExit) as caught:
                macos.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn(str(primary), stderr.getvalue())
            self.assertNotIn("foreign owner cleanup failure", stderr.getvalue())
            self.assertNotIn("global stop failure", stderr.getvalue())
            cleanup.assert_not_called()
            stop.assert_not_called()

    def test_direct_launch_cannot_bypass_cli_admission(self) -> None:
        fixture = load_profile(V2_PROFILE, "10", "ab")
        for command in set(macos.EXTERNAL_RF_COMMANDS.values()) | set(
            macos.RF_SCENARIO_COMMANDS.values()
        ):
            for configuration in (None, fixture):
                with (
                    self.subTest(command=command, fixture=configuration),
                    ExitStack() as stack,
                ):
                    actions = [
                        stack.enter_context(
                            patch.object(macos, "_required_executable")
                        ),
                        stack.enter_context(patch.object(macos, "_run")),
                        stack.enter_context(
                            patch.object(macos, "_run_notification_coordination")
                        ),
                        stack.enter_context(
                            patch.object(
                                macos, "_run_provider_passive_disconnect_coordination"
                            )
                        ),
                        stack.enter_context(
                            patch.object(macos, "_run_passive_disconnect_coordination")
                        ),
                        stack.enter_context(patch.object(macos.shutil, "rmtree")),
                        stack.enter_context(patch.object(macos.Path, "unlink")),
                        stack.enter_context(patch.object(macos.Path, "mkdir")),
                        stack.enter_context(
                            patch.object(macos.Path, "is_file", return_value=False)
                        ),
                        stack.enter_context(patch.object(macos.uuid, "uuid4")),
                    ]
                    with self.assertRaisesRegex(RuntimeError, "macOS RF blocked"):
                        macos._launch("open", command, configuration)
                    for action in actions:
                        action.assert_not_called()

    def test_non_rf_build_runtime_and_authorize_dispatch_remain_available(self) -> None:
        for command in ("build", "runtime", "authorize"):
            with self.subTest(command=command), ExitStack() as stack:
                stack.enter_context(patch.object(macos.sys, "platform", "darwin"))
                stack.enter_context(
                    patch.object(macos.sys, "argv", ["macos-probe", command])
                )
                stack.enter_context(
                    patch.object(
                        macos, "_required_executable", side_effect=lambda name: name
                    )
                )
                build = stack.enter_context(patch.object(macos, "_build_bundle"))
                launch = stack.enter_context(patch.object(macos, "_launch"))
                run = stack.enter_context(patch.object(macos, "_run"))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(macos.main(), 0)
                build.assert_called_once_with("swift", "codesign", "plutil")
                if command == "authorize":
                    launch.assert_called_once_with("open", "authorize")
                    run.assert_not_called()
                elif command == "runtime":
                    run.assert_called_once_with(
                        [str(macos.BUNDLE_EXECUTABLE)], timeout=45
                    )
                    launch.assert_not_called()
                else:
                    launch.assert_not_called()
                    run.assert_not_called()


class MacosRfRecipeSafetyTests(unittest.TestCase):
    def recipes(self) -> dict[str, tuple[str, list[str]]]:
        """Read headers and bodies without executing even a Just prerequisite."""
        result: dict[str, tuple[str, list[str]]] = {}
        header = ""
        name = ""
        for line in (
            (ROOT / "just/apple/mod.just").read_text(encoding="utf-8").splitlines()
        ):
            if line and not line[0].isspace():
                match = re.match(r"([a-z][a-z0-9-]*)(?:\s|:)", line)
                if match and ":" in line:
                    name, header = match[1], line
                    result[name] = (header, [])
                else:
                    name = ""
            elif name and line.strip():
                result[name][1].append(line.strip())
        return result

    def test_shortcuts_have_no_prepare_provider_or_cleanup_side_effects(self) -> None:
        recipes = self.recipes()
        for name, scenario in {
            **SHORTCUTS,
            "rf-macos-external": "{{quote(scenario)}}",
            "rf-macos-android": "{{quote(scenario)}}",
        }.items():
            with self.subTest(recipe=name):
                header, body = recipes[name]
                self.assertEqual(header.rsplit(":", 1)[1].strip(), "")
                self.assertNotIn("blehub-nrf-indication-v2.json", header)
                self.assertEqual(len(body), 1)
                self.assertTrue(
                    body[0].startswith(
                        "{{apple_run}} python -m tools.apple.test_macos_runtime rf-"
                    )
                )
                self.assertIn(scenario, body[0])
                for forbidden in (
                    "trap",
                    "peripheral",
                    "direct-start",
                    "stop",
                    "rm ",
                    "mkdir",
                    "|| true",
                    "2>&1",
                ):
                    self.assertNotIn(forbidden, body[0])

    def test_all_preserves_strict_indication_without_build_or_provider_dependency(
        self,
    ) -> None:
        header, body = self.recipes()["rf-macos-nrf52840-all"]
        self.assertEqual(header.rsplit(":", 1)[1].strip(), "")
        self.assertEqual(body, [f"pixi run just apple {name}" for name in SHORTCUTS])

    def test_canonical_host_discovery_includes_safety_tests_without_prepare(
        self,
    ) -> None:
        header, body = self.recipes()["test-rf-host"]
        self.assertEqual(header, "test-rf-host:")
        self.assertEqual(len(body), 1)
        self.assertIn("tools.apple.test_rf_fixture", body[0])
        self.assertIn("tools.apple.test_macos_rf_safety", body[0])


if __name__ == "__main__":
    unittest.main()
