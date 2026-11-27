"""Hardware-free regressions for the formal-App adapter OFF -> ON lane.

Two things can silently rot here without hardware: the stage contract (Python and Kotlin
drifting apart, or the radio-restore moment moving) and the host boundaries (a bound or an
authorization that stops being refused before a lease, an install or a radio mutation).
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.react_native import bleng_android_adapter_transition as lane
from tools.react_native import run_bleng_android_adapter_transition as cli

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KOTLIN_TEST = (
    PROJECT_ROOT
    / "tests/consumers/react-native-bleng/android/app/src/androidTest/kotlin"
    / "com/blehubblenghost/BlengAdapterTransitionInstrumentedTest.kt"
)


def _kotlin_markers(source: str) -> tuple[str, ...]:
    """The `marker("...")` literals in emission order."""
    return tuple(re.findall(r'marker\("([^"]+)"\)', source))


class StageContractTests(unittest.TestCase):
    def test_the_vocabulary_is_ordered_and_bounded(self):
        stages = lane.EXPECTED_STAGES
        self.assertEqual(len(stages), len(set(stages)))
        self.assertEqual(stages[0], lane.REFUSAL_STAGE)
        self.assertEqual(stages[-1], "done")

    def test_the_radio_returns_only_after_the_app_reported_its_refusal(self):
        stages = lane.EXPECTED_STAGES
        # The host flips the radio when it reads the refusal. The very next product fact is the
        # adapter lane reporting the restored radio — the change arrived, not just a retry that
        # happened to succeed — and only then does the App's own retry run.
        self.assertEqual(
            stages.index(lane.REFUSAL_STAGE) + 1, stages.index(lane.STREAM_STAGE)
        )
        self.assertLess(
            stages.index(lane.STREAM_STAGE), stages.index(lane.RECOVERY_STAGE)
        )
        # Nothing in the product part of the scene may precede the refusal it depends on.
        self.assertLess(
            stages.index(lane.RECOVERY_STAGE), stages.index("scan-submitted")
        )

    def test_the_kotlin_test_emits_exactly_the_declared_vocabulary(self):
        self.assertTrue(
            KOTLIN_TEST.is_file(), f"missing instrumented test: {KOTLIN_TEST}"
        )
        markers = _kotlin_markers(KOTLIN_TEST.read_text(encoding="utf-8"))
        self.assertEqual(
            markers,
            lane.EXPECTED_STAGES,
            "the instrumented test and the host stage contract have drifted apart",
        )


class AdmissionTests(unittest.TestCase):
    """Every host-side bound is refused before a lease, a phone or a radio mutation."""

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
        return lane.run_adapter_transition(**arguments)  # type: ignore[arg-type]

    def test_authorization_is_explicit_and_never_defaulted(self):
        for name in (
            "authorize_install",
            "authorize_fixture_start",
            "authorize_bluetooth_permissions",
        ):
            with self.subTest(field=name), self.assertRaises(ValueError):
                self.run_lane(**{name: False})
            with (
                self.subTest(field=name, value="truthy"),
                self.assertRaises(ValueError),
            ):
                self.run_lane(**{name: "yes"})

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

    def test_a_blank_physical_serial_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_lane(device="   ")

    def test_a_port_alias_is_not_a_stable_fixture_identity(self):
        for identity in ("jlink:0", "usb:1050252028", "jlink:abc", ""):
            with (
                self.subTest(identity=identity),
                self.assertRaises((ValueError, RuntimeError)),
            ):
                self.run_lane(fixture_resource_identity=identity)

    def test_missing_fixture_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            self.run_lane(fixture_contract=self.root / "absent.json")
        with self.assertRaises(ValueError):
            self.run_lane(fixture_profile=self.root / "absent.json")

    def test_a_digest_that_is_not_a_sha256_is_refused(self):
        for name in ("app_apk_sha256", "test_apk_sha256"):
            for value in ("ABC", "A" * 64, "0" * 63, 0):
                with (
                    self.subTest(field=name, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.run_lane(**{name: value})

    def test_an_apk_that_is_not_the_authorized_binary_is_refused(self):
        (self.root / "app.apk").write_bytes(b"not the frozen App")
        (self.root / "test.apk").write_bytes(b"not the frozen tests")
        with self.assertRaises(ValueError) as raised:
            self.run_lane()
        self.assertIn("does not match", str(raised.exception))

    def test_no_admission_failure_ever_touches_the_phone(self):
        with mock.patch.object(lane, "AndroidRadioController") as controller:
            for overrides in (
                {"device": "emulator-5554"},
                {"authorize_install": False},
                {"timeout": 0},
                {"fixture_resource_identity": "jlink:0"},
            ):
                with (
                    self.subTest(overrides=overrides),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    self.run_lane(**overrides)
        controller.assert_not_called()


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
                mock.patch.object(cli, "run_adapter_transition") as run,
            ):
                arguments = self.base_arguments()
                arguments.remove(flag)
                with self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()

    def test_an_upper_case_or_short_digest_is_refused_before_any_work(self):
        with mock.patch.object(cli, "run_adapter_transition") as run:
            for digest in ("A" * 64, "0" * 63):
                with self.subTest(digest=digest):
                    arguments = self.base_arguments()
                    arguments[arguments.index("0" * 64)] = digest
                    with self.assertRaises(SystemExit) as raised:
                        cli.main(arguments)
                    self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_a_blank_fixture_port_is_refused_before_any_work(self):
        with mock.patch.object(cli, "run_adapter_transition") as run:
            arguments = self.base_arguments()
            arguments[arguments.index("/dev/cu.usbmodem0010502520281")] = " "
            with self.assertRaises(SystemExit) as raised:
                cli.main(arguments)
            self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
