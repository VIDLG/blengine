"""Hardware-free regressions for the three-phase permission transition lane.

The lane's hard parts are checkable without a phone: the per-phase stage contract (Python
and Kotlin drifting apart, or a phase silently gaining work), the process-identity gate
(A wrong package/uid/field would make the OS death proof meaningless), and the host
admission bounds (a refusal must happen before a lease, an install or a permission change).
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.react_native import bleng_android_permission_transition as lane
from tools.react_native import run_bleng_android_permission_transition as cli

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KOTLIN_TEST = (
    PROJECT_ROOT
    / "tests/consumers/react-native-bleng/android/app/src/androidTest/kotlin"
    / "com/blehubblenghost/BlengPermissionTransitionInstrumentedTest.kt"
)


def _kotlin_stage_names(source: str) -> list[str]:
    """The `marker("...")` literals as stage keys (a colon only introduces a payload)."""
    names = []
    for literal in re.findall(r'marker\("([^"]*)"', source):
        names.append(literal.split(":", 1)[0])
    return names


def _identity(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "pid": 4242,
        "uid": 10123,
        "user_id": 0,
        "process_name": lane.APP_PACKAGE,
        "start_ticks": 9001,
        "native_started_unix_ms": 1_700_000_000_000,
    }
    value.update(overrides)
    return value


def _bootstrap_marker(payload: object) -> list[str]:
    return ["bootstrap:" + json.dumps(payload)]


class StageContractTests(unittest.TestCase):
    def test_every_phase_starts_with_its_identity_marker(self):
        for phase, (_, stages) in lane.SCENES.items():
            with self.subTest(phase=phase):
                self.assertEqual(stages[0], "bootstrap")
                self.assertEqual(len(stages), len(set(stages)))

    def test_the_phases_are_disjoint_and_the_death_phase_has_no_terminal(self):
        self.assertEqual(tuple(lane.SCENES), lane.PHASES)
        self.assertNotIn("done", lane.SCENES["active"][1])
        self.assertNotIn("restored-io", lane.SCENES["denied"][1])
        self.assertNotIn("denied-scan", lane.SCENES["restored"][1])

    def test_every_phase_names_its_own_method(self):
        methods = [method for method, _ in lane.SCENES.values()]
        self.assertEqual(len(methods), len(set(methods)))

    def test_the_kotlin_test_emits_exactly_the_declared_vocabulary(self):
        self.assertTrue(
            KOTLIN_TEST.is_file(), f"missing instrumented test: {KOTLIN_TEST}"
        )
        observed = _kotlin_stage_names(KOTLIN_TEST.read_text(encoding="utf-8"))
        declared = [stage for _, stages in lane.SCENES.values() for stage in stages]
        # The Kotlin file contains only the contract markers plus one host-routing marker that
        # must never be reached ("unexpected-held-read-terminal").
        self.assertEqual(
            set(observed) - {"unexpected-held-read-terminal"},
            set(declared),
            "the instrumented test and the host stage contract have drifted apart",
        )
        self.assertEqual(observed.count("bootstrap"), len(lane.PHASES))
        self.assertEqual(observed.count("unexpected-held-read-terminal"), 1)


class IdentityTests(unittest.TestCase):
    def test_a_complete_bootstrap_marker_is_the_process_identity(self):
        value = _identity()
        identity = lane._phase_identity(
            _bootstrap_marker(value), phase="denied", uid=10123
        )
        self.assertEqual(identity, value)

    def test_the_marker_must_belong_to_this_package_user0_and_uid(self):
        for overrides in (
            {"process_name": "com.example.other"},
            {"uid": 10124},
            {"user_id": 1},
            {"pid": 0},
            {"start_ticks": 0},
            {"native_started_unix_ms": 0},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                lane._phase_identity(
                    _bootstrap_marker(_identity(**overrides)),
                    phase="denied",
                    uid=10123,
                )

    def test_integer_fields_must_be_integers(self):
        for name in lane._IDENTITY_INTEGERS:
            with self.subTest(field=name), self.assertRaises(RuntimeError):
                lane._phase_identity(
                    _bootstrap_marker(_identity(**{name: "1"})),
                    phase="denied",
                    uid=10123,
                )

    def test_missing_extra_malformed_and_duplicate_markers_are_refused(self):
        missing = _identity()
        missing.pop("start_ticks")
        cases = (
            _bootstrap_marker(missing),
            _bootstrap_marker({**_identity(), "extra": 1}),
            ["bootstrap:{not-json"],
            _bootstrap_marker(_identity()) * 2,
            [],
        )
        for stages in cases:
            with self.subTest(stages=stages), self.assertRaises(RuntimeError):
                lane._phase_identity(stages, phase="denied", uid=10123)

    def test_a_reused_process_instance_is_refused(self):
        first = _identity()
        lane._require_fresh_identity(
            {"denied": first}, _identity(pid=5000), phase="active"
        )
        with self.assertRaises(RuntimeError):
            lane._require_fresh_identity({"denied": first}, dict(first), phase="active")
        # PID reuse is still a different instance when start_ticks differ.
        lane._require_fresh_identity(
            {"denied": first}, _identity(start_ticks=9002), phase="active"
        )


class AdmissionTests(unittest.TestCase):
    """Every host-side bound is refused before a lease, a phone or a permission change."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.contract, self.profile = write_v2_inputs(self.root, "a")

    def run_lane(self, **overrides: object) -> object:
        arguments: dict[str, object] = {
            "device": "3ac5cc09",
            "app_apk": self.root / "app.apk",
            "app_apk_sha256": "0" * 64,
            "test_apk": self.root / "test.apk",
            "test_apk_sha256": "0" * 64,
            "fixture_port": "/dev/cu.usbmodem0010502520281",
            "fixture_contract": self.contract,
            "fixture_profile": self.profile,
            "fixture_resource_identity": "jlink:1050252028",
            "output_root": self.root / "out",
            "timeout": 300.0,
            "cleanup_seconds": 60.0,
            "authorize_install": True,
            "authorize_fixture_start": True,
            "authorize_bluetooth_permissions": True,
            "use_root": True,
            "cwd": PROJECT_ROOT,
        }
        arguments.update(overrides)
        return lane.run_permission_transition(**arguments)  # type: ignore[arg-type]

    def test_authorization_is_explicit_and_never_defaulted(self):
        for name in (
            "authorize_install",
            "authorize_fixture_start",
            "authorize_bluetooth_permissions",
        ):
            for value in (False, "yes"):
                with (
                    self.subTest(field=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.run_lane(**{name: value})

    def test_budgets_are_finite_and_bounded(self):
        for name, value in (
            ("timeout", 0),
            ("timeout", lane.SYSTEM_TIMEOUT_MAX + 1),
            ("timeout", float("inf")),
            ("cleanup_seconds", 0),
            ("cleanup_seconds", lane.CLEANUP_TIMEOUT_MAX + 1),
        ):
            with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                self.run_lane(**{name: value})

    def test_an_emulator_serial_can_never_satisfy_the_physical_bound(self):
        with self.assertRaises(ValueError):
            self.run_lane(device="emulator-5554")
        with self.assertRaises(ValueError):
            self.run_lane(device="   ")

    def test_a_port_alias_is_not_a_stable_fixture_identity(self):
        for identity in ("jlink:0", "usb:1050252028", "jlink:abc", ""):
            with (
                self.subTest(identity=identity),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                self.run_lane(fixture_resource_identity=identity)

    def test_missing_fixture_inputs_and_apks_are_refused(self):
        with self.assertRaises(ValueError):
            self.run_lane(fixture_contract=self.root / "absent.json")
        with self.assertRaises(ValueError):
            self.run_lane(fixture_profile=self.root / "absent.json")
        with self.assertRaises(ValueError):
            self.run_lane()

    def test_a_digest_that_is_not_a_sha256_is_refused(self):
        for name in ("app_apk_sha256", "test_apk_sha256"):
            for value in ("ABC", "A" * 64, "0" * 63, 0):
                with (
                    self.subTest(field=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.run_lane(**{name: value})

    def test_no_admission_failure_ever_touches_the_phone_or_permissions(self):
        with (
            mock.patch.object(lane, "AndroidRadioController") as radio,
            mock.patch.object(lane, "AndroidPermissionController") as permissions,
        ):
            for overrides in (
                {"device": "emulator-5554"},
                {"authorize_bluetooth_permissions": False},
                {"timeout": 0},
                {"fixture_resource_identity": "jlink:0"},
            ):
                with (
                    self.subTest(overrides=overrides),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    self.run_lane(**overrides)
        radio.assert_not_called()
        permissions.assert_not_called()


class CommandLineTests(unittest.TestCase):
    def base_arguments(self) -> list[str]:
        return [
            "--device",
            "3ac5cc09",
            "--app-apk",
            "/tmp/app.apk",
            "--app-apk-sha256",
            "0" * 64,
            "--test-apk",
            "/tmp/test.apk",
            "--test-apk-sha256",
            "0" * 64,
            "--fixture-port",
            "/dev/cu.usbmodem0010502520281",
            "--fixture-contract",
            "/tmp/contract.json",
            "--fixture-profile",
            "/tmp/profile.json",
            "--fixture-resource-identity",
            "jlink:1050252028",
            "--output",
            "/tmp/out",
            "--authorize-install",
            "--authorize-fixture-start",
            "--authorize-bluetooth-permissions",
        ]

    def test_the_declared_defaults_sit_inside_the_lane_bounds(self):
        args = cli.build_parser().parse_args(self.base_arguments())
        self.assertEqual(args.timeout, 900)
        self.assertEqual(args.cleanup_seconds, 180)
        self.assertLessEqual(args.timeout, lane.SYSTEM_TIMEOUT_MAX)
        self.assertLessEqual(args.cleanup_seconds, lane.CLEANUP_TIMEOUT_MAX)
        self.assertFalse(args.use_root)

    def test_each_authorization_flag_is_required(self):
        for flag in (
            "--authorize-install",
            "--authorize-fixture-start",
            "--authorize-bluetooth-permissions",
        ):
            with (
                self.subTest(flag=flag),
                mock.patch.object(cli, "run_permission_transition") as run,
            ):
                arguments = self.base_arguments()
                arguments.remove(flag)
                with self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()

    def test_an_upper_case_or_short_digest_is_refused_before_any_work(self):
        with mock.patch.object(cli, "run_permission_transition") as run:
            for digest in ("A" * 64, "0" * 63):
                with self.subTest(digest=digest):
                    arguments = self.base_arguments()
                    arguments[arguments.index("0" * 64)] = digest
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(arguments)
                    self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_a_blank_fixture_port_is_refused_before_any_work(self):
        with mock.patch.object(cli, "run_permission_transition") as run:
            arguments = self.base_arguments()
            arguments[arguments.index("/dev/cu.usbmodem0010502520281")] = " "
            with self.assertRaises(SystemExit) as raised:
                cli.main(arguments)
            self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
