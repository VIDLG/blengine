"""Hardware-free regressions for the iOS permission-authorization UI lane.

What can rot without a phone: the fresh-install precondition (a phase that does not remove the App
can never see the OS consent alert), the per-phase environment, the exact `-only-testing` identity,
and the verdict rule (a skipped or failed test must never become a pass).
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.react_native import bleng_ios_permission_ui as lane
from tools.react_native import run_bleng_ios_permission_ui as cli


def _summary(**overrides: object) -> str:
    value: dict[str, object] = {
        "totalTestCount": 1,
        "passedTests": 1,
        "failedTests": 0,
        "skippedTests": 0,
    }
    value.update(overrides)
    return json.dumps(value)


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.xctestrun = self.root / "lane.xctestrun"
        self.xctestrun.write_bytes(b"manifest")

    def run_lane(self, **overrides: object) -> object:
        arguments: dict[str, object] = {
            "device": "00008110-000C61160C39401E",
            "xctestrun": self.xctestrun,
            "output_root": self.root / "out",
            "timeout": 300.0,
            "restore_only": False,
            "authorize_permission_reset": True,
            "cwd": self.root,
        }
        arguments.update(overrides)
        return lane.run_permission_ui(**arguments)  # type: ignore[arg-type]

    def test_the_reinstall_authorization_cannot_be_defaulted(self):
        for value in (False, "yes", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_lane(authorize_permission_reset=value)

    def test_restore_only_is_an_explicit_bool(self):
        for value in (0, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_lane(restore_only=value)

    def test_budgets_are_finite_and_bounded(self):
        for value in (0, lane.TIMEOUT_MAX + 1, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_lane(timeout=value)

    def test_the_device_and_manifest_are_explicit(self):
        with self.assertRaises(ValueError):
            self.run_lane(device="  ")
        with self.assertRaises(ValueError):
            self.run_lane(xctestrun=self.root / "absent.xctestrun")

    def test_no_admission_failure_touches_the_device(self):
        with (
            mock.patch.object(lane.ios_sdk, "run_command") as run,
            mock.patch.object(lane, "_uninstall_app") as uninstall,
        ):
            for overrides in (
                {"authorize_permission_reset": False},
                {"timeout": 0},
                {"device": ""},
                {"restore_only": "1"},
            ):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    self.run_lane(**overrides)
        run.assert_not_called()
        uninstall.assert_not_called()


class PhaseTests(unittest.TestCase):
    """The runner owns the uninstall/run order and the per-phase environment."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.xctestrun = self.root / "lane.xctestrun"
        self.xctestrun.write_bytes(b"manifest")
        self.run_id = str(uuid.uuid4())
        self.resolved = SimpleNamespace(
            name="BleHubBlengHostUITests",
            document={
                "BleHubBlengHostUITests": {
                    "UITargetAppPath": str(self.root / "BleHubBlengHost.app")
                }
            },
        )

    def run_with(self, *, restore_only: bool = False, summary: str | None = None):
        run_id = str(uuid.uuid4())
        events: list[str] = []
        environments: list[dict[str, str]] = []

        def fake_uninstall(device, bundle_id, *, cwd, deadline):
            events.append(f"uninstall:{bundle_id}")
            return {"bundle_id": bundle_id, "removed": True}

        def fake_command(command: list[str], **_: object) -> SimpleNamespace:
            joined = " ".join(command)
            if "xcodebuild" in joined:
                result = Path(command[command.index("-resultBundlePath") + 1])
                result.mkdir(parents=True, exist_ok=True)
                events.append(f"run:{result.stem}")
                return SimpleNamespace(returncode=0, stdout="** TEST SUCCEEDED **\n")
            return SimpleNamespace(returncode=0, stdout=summary or _summary())

        def fake_stage(source, destination, *, test_bundle, environment, **_: object):
            environments.append(dict(environment))
            return destination

        with (
            mock.patch.object(
                lane.ios_preflight,
                "preflight",
                return_value={
                    "status": "verified",
                    "observations": {
                        "ui-test-runner-inventory": {
                            "expected_runner_bundle_id": "team.host.xctrunner"
                        }
                    },
                },
            ),
            mock.patch.object(lane, "resolve_xctestrun", return_value=self.resolved),
            mock.patch.object(lane, "app_bundle_id", return_value="TEAM.host"),
            mock.patch.object(lane, "staged_xctestrun", side_effect=fake_stage),
            mock.patch.object(lane, "_uninstall_app", side_effect=fake_uninstall),
            mock.patch.object(lane.ios_sdk, "run_command", side_effect=fake_command),
        ):
            path = lane.run_permission_ui(
                device="00008110-000C61160C39401E",
                xctestrun=self.xctestrun,
                output_root=self.root / "out",
                timeout=300.0,
                restore_only=restore_only,
                authorize_permission_reset=True,
                cwd=self.root,
                run_id=run_id,
            )
        return path, events, environments

    def test_deny_runs_before_allow_and_each_phase_reinstalls(self):
        path, events, environments = self.run_with()
        value = json.loads(path.read_bytes())
        self.assertEqual(value["status"], "passed")
        self.assertEqual(set(value["phases"]), {"deny", "allow"})
        self.assertEqual(
            events,
            [
                "uninstall:TEAM.host",
                "run:deny",
                "uninstall:TEAM.host",
                "run:allow",
            ],
        )
        self.assertEqual(
            environments,
            [
                {"BLEHUB_IOS_PERMISSION_PHASE": "deny"},
                {"BLEHUB_IOS_PERMISSION_PHASE": "allow"},
            ],
        )
        self.assertEqual(
            value["only_testing"],
            "BleHubBlengHostUITests/BlengPermissionAuthorizationUITests/"
            "testBluetoothConsentPhase",
        )

    def test_restore_only_runs_the_allow_phase_alone(self):
        path, events, environments = self.run_with(restore_only=True)
        value = json.loads(path.read_bytes())
        self.assertEqual(value["status"], "passed")
        self.assertEqual(set(value["phases"]), {"allow"})
        self.assertEqual(events, ["uninstall:TEAM.host", "run:allow"])
        self.assertEqual(environments, [{"BLEHUB_IOS_PERMISSION_PHASE": "allow"}])

    def test_a_skipped_or_failed_phase_can_never_be_a_pass(self):
        for summary in (
            _summary(skippedTests=1, passedTests=0),
            _summary(failedTests=1, passedTests=0),
            _summary(totalTestCount=2),
            _summary(passedTests=0),
        ):
            with self.subTest(summary=summary), self.assertRaises(RuntimeError):
                self.run_with(summary=summary)

    def test_a_blocked_preflight_refuses_before_any_uninstall(self):
        with (
            mock.patch.object(
                lane.ios_preflight,
                "preflight",
                return_value={
                    "status": "blocked",
                    "observations": {
                        "ui-test-runner-inventory": {"status": "blocked"},
                    },
                },
            ),
            mock.patch.object(lane, "resolve_xctestrun", return_value=self.resolved),
            mock.patch.object(lane, "app_bundle_id", return_value="TEAM.host"),
            mock.patch.object(lane, "_uninstall_app") as uninstall,
            mock.patch.object(lane.ios_sdk, "run_command") as run,
            self.assertRaises(RuntimeError) as raised,
        ):
            lane.run_permission_ui(
                device="00008110-000C61160C39401E",
                xctestrun=self.xctestrun,
                output_root=self.root / "out",
                timeout=300.0,
                restore_only=False,
                authorize_permission_reset=True,
                cwd=self.root,
                run_id=self.run_id,
            )
        self.assertIn("ui-test-runner-inventory", str(raised.exception))
        uninstall.assert_not_called()
        run.assert_not_called()


class CommandLineTests(unittest.TestCase):
    def base_arguments(self) -> list[str]:
        return [
            "--device",
            "00008110-000C61160C39401E",
            "--xctestrun",
            "/tmp/lane.xctestrun",
            "--output",
            "/tmp/out",
            "--authorize-permission-reset",
        ]

    def test_the_declared_defaults_are_bounded(self):
        args = cli.build_parser().parse_args(self.base_arguments())
        self.assertEqual(args.timeout, 900)
        self.assertLessEqual(args.timeout, lane.TIMEOUT_MAX)
        self.assertFalse(args.restore_only)

    def test_the_reset_authorization_is_required(self):
        with mock.patch.object(cli, "run_permission_ui") as run:
            arguments = self.base_arguments()
            arguments.remove("--authorize-permission-reset")
            with self.assertRaises(SystemExit) as raised:
                cli.main(arguments)
            self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_a_refused_lane_is_reported_as_failure(self):
        with mock.patch.object(
            cli, "run_permission_ui", side_effect=ValueError("refused")
        ):
            self.assertEqual(cli.main(self.base_arguments()), 2)


if __name__ == "__main__":
    unittest.main()
