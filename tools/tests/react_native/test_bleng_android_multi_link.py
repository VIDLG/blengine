"""Hardware-free regressions for the multi-link scene's host boundaries.

The claim of this lane is per-link isolation, so the two things that can silently rot are
the stage contract (Python and Kotlin drifting apart) and the per-peer ownership rules
(one peer's failure releasing another peer's resources). Both are checkable without hardware.
"""

from __future__ import annotations

import re
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.react_native import bleng_android_multi_link as lane
from tools.react_native import run_bleng_android_multi_link as cli
from tools.react_native.bleng_multi_link_contract import FAULT_WRITE_REJECT, Fault

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KOTLIN_TEST = (
    PROJECT_ROOT
    / "tests/consumers/react-native-bleng/android/app/src/androidTest/kotlin"
    / "com/blehubblenghost/BlengMultiLinkInstrumentedTest.kt"
)


def _python_stems(stages: tuple[str, ...]) -> set[str]:
    """Collapse the per-link index so one vocabulary can be compared with another."""
    return {re.sub(r":\d+$", ":{n}", stage) for stage in stages}


def _kotlin_stems(source: str) -> set[str]:
    """Every `marker("...")` literal, with `$index` and fixed indices collapsed."""
    stems = set()
    for literal in re.findall(r'marker\("([^"]+)"\)', source):
        normalised = literal.replace("$index", "{n}")
        stems.add(re.sub(r":\d+$", ":{n}", normalised))
    return stems


class StageContractTests(unittest.TestCase):
    def test_the_stage_vocabulary_is_ordered_and_bounded(self):
        for count in range(lane.MINIMUM_PEERS, lane.MAXIMUM_PEERS + 1):
            with self.subTest(peers=count):
                stages = lane.expected_stages(count)
                self.assertEqual(len(stages), len(set(stages)))
                self.assertEqual(stages[0], "started")
                self.assertEqual(stages[-1], "done")
                self.assertLess(stages.index("scan-stopped"), stages.index("done"))

    def test_every_peer_is_exercised_individually(self):
        stages = lane.expected_stages(3)
        for index in range(3):
            for stem in ("advertisement", "connected", "gatt", "written", "io"):
                self.assertIn(f"{stem}:{index}", stages)
            self.assertIn(f"subscribed:{index}", stages)
            self.assertIn(f"value:{index}", stages)

    def test_the_disturbed_link_is_the_first_and_never_itself_a_survivor(self):
        stages = lane.expected_stages(3)
        self.assertIn("drop-requested:0", stages)
        self.assertIn("dropped:0", stages)
        self.assertNotIn("survivor-requested:0", stages)
        self.assertNotIn("survivor:0", stages)
        # Survivors are contiguous in the contract: every peer above the disturbed one.
        for index in (1, 2):
            self.assertIn(f"survivor-requested:{index}", stages)
            self.assertIn(f"survivor:{index}", stages)
            self.assertIn(f"disconnected:{index}", stages)
            self.assertLess(
                stages.index(f"survivor-requested:{index}"),
                stages.index(f"survivor:{index}"),
            )
            self.assertLess(
                stages.index(f"survivor:{index}"), stages.index(f"disconnected:{index}")
            )
        # The disturbed link is never disconnected by this side: the peer ended it.
        self.assertNotIn("disconnected:0", stages)

    def test_the_two_peer_contract_is_the_minimum_case(self):
        stages = lane.expected_stages(2)
        self.assertEqual(
            stages[-6:],
            (
                "drop-requested:0",
                "dropped:0",
                "survivor-requested:1",
                "survivor:1",
                "disconnected:1",
                "done",
            ),
        )

    def test_the_kotlin_test_emits_exactly_the_declared_vocabulary(self):
        self.assertTrue(
            KOTLIN_TEST.is_file(), f"missing instrumented test: {KOTLIN_TEST}"
        )
        source = KOTLIN_TEST.read_text(encoding="utf-8")
        # The lane carries both the normal and the faulted branch for every link, so its stem set
        # is the union of the two vocabularies — an exact comparison, not a subset check.
        expected = _python_stems(lane.expected_stages(2)) | _python_stems(
            lane.expected_stages(2, fault=Fault(FAULT_WRITE_REJECT, 0))
        )
        self.assertEqual(
            _kotlin_stems(source),
            expected,
            "the instrumented test and the host stage contract have drifted apart",
        )


class WritePayloadTests(unittest.TestCase):
    def test_each_link_writes_a_value_no_other_link_could_produce(self):
        provisioned = bytes.fromhex("00112233445566778899aabbccddeeff")
        payloads = [lane._write_payload(index, provisioned) for index in range(4)]
        self.assertEqual(len(set(payloads)), len(payloads))
        for index, payload in enumerate(payloads):
            self.assertEqual(len(payload), len(provisioned))
            self.assertNotEqual(
                payload, provisioned, f"link {index} would write the fixture value"
            )
            self.assertEqual(payload[1:], provisioned[1:])

    def test_an_empty_provisioned_value_is_a_profile_defect(self):
        with self.assertRaises(ValueError):
            lane._write_payload(0, b"")

    def test_a_payload_that_cannot_differ_is_refused(self):
        # The head byte is forced, so a fixture whose first byte already matches cannot be
        # distinguished from a stale read: that is a refusal, not a silent pass.
        with self.assertRaises(ValueError):
            lane._write_payload(0, bytes([0xA0]))


class SessionIdentityTests(unittest.TestCase):
    run_id = str(uuid.uuid4())

    def test_each_peer_gets_its_own_canonical_session(self):
        ids = [lane.peer_session_id(self.run_id, index) for index in range(4)]
        self.assertEqual(len(set(ids)), len(ids))
        for value in ids:
            self.assertEqual(str(uuid.UUID(value)), value)

    def test_the_identity_is_derived_from_the_run_not_drawn_fresh(self):
        first = lane.peer_session_id(self.run_id, 1)
        self.assertEqual(first, lane.peer_session_id(self.run_id, 1))
        self.assertNotEqual(first, lane.peer_session_id(str(uuid.uuid4()), 1))


class FixtureSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.contract, self.profile = write_v2_inputs(self.root, "a")

    def spec(self, **overrides: object) -> lane.FixtureSpec:
        fields: dict[str, object] = {
            "port": "/dev/cu.usbmodem001",
            "contract": self.contract,
            "profile": self.profile,
            "resource_identity": "jlink:1050252028",
        }
        fields.update(overrides)
        return lane.FixtureSpec(**fields)  # type: ignore[arg-type]

    def test_a_declared_peer_needs_a_port_identity_and_real_inputs(self):
        self.assertEqual(self.spec().resource_identity, "jlink:1050252028")
        with self.assertRaises(ValueError):
            self.spec(port="")
        with self.assertRaises(ValueError):
            self.spec(contract=self.root / "absent.json")
        with self.assertRaises(ValueError):
            self.spec(profile=self.root / "absent.json")

    def test_a_port_alias_is_not_a_stable_identity(self):
        with self.assertRaises(RuntimeError):
            self.spec(resource_identity="jlink:0")
        with self.assertRaises(RuntimeError):
            self.spec(resource_identity="usb:1050252028")


class AdmissionTests(unittest.TestCase):
    """Every host-side bound is refused before a lease, a phone or a write can happen."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.contract, self.profile = write_v2_inputs(self.root, "a")

    def peer(self, index: int, **overrides: object) -> lane.FixtureSpec:
        fields: dict[str, object] = {
            "port": f"/dev/cu.usbmodem00105025{index}",
            "contract": self.contract,
            "profile": self.profile,
            "resource_identity": f"jlink:105025202{index}",
        }
        fields.update(overrides)
        return lane.FixtureSpec(**fields)  # type: ignore[arg-type]

    def run_lane(self, fixtures: list[lane.FixtureSpec], **overrides: object) -> object:
        arguments: dict[str, object] = {
            "device": "3ac5cc09",
            "app_apk": self.root / "app.apk",
            "app_apk_sha256": "0" * 64,
            "test_apk": self.root / "test.apk",
            "test_apk_sha256": "0" * 64,
            "fixtures": fixtures,
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
        return lane.run_multi_link(**arguments)  # type: ignore[arg-type]

    def test_a_single_link_is_not_a_multi_link_scene(self):
        with self.assertRaises(ValueError):
            self.run_lane([self.peer(0)])
        with self.assertRaises(ValueError):
            self.run_lane([])

    def test_more_peers_than_the_lane_declares_are_refused(self):
        with self.assertRaises(ValueError):
            self.run_lane([self.peer(index) for index in range(lane.MAXIMUM_PEERS + 1)])

    def test_two_peers_may_not_share_a_port_or_an_identity(self):
        with self.assertRaises(ValueError):
            self.run_lane(
                [self.peer(0), self.peer(0, resource_identity="jlink:1050252099")]
            )
        with self.assertRaises(ValueError):
            self.run_lane(
                [self.peer(0), self.peer(0, port="/dev/cu.usbmodem0010502599")]
            )

    def test_authorization_is_explicit_and_never_defaulted(self):
        for name in (
            "authorize_install",
            "authorize_fixture_start",
            "authorize_bluetooth_permissions",
        ):
            with self.subTest(field=name), self.assertRaises(ValueError):
                self.run_lane([self.peer(0), self.peer(1)], **{name: False})

    def test_budgets_are_finite_and_bounded(self):
        for name, value in (
            ("timeout", 0),
            ("timeout", 901),
            ("timeout", float("inf")),
            ("cleanup_seconds", 0),
            ("cleanup_seconds", 181),
        ):
            with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                self.run_lane([self.peer(0), self.peer(1)], **{name: value})

    def test_an_emulator_serial_can_never_satisfy_the_physical_bound(self):
        with self.assertRaises(ValueError):
            self.run_lane([self.peer(0), self.peer(1)], device="emulator-5554")

    def test_a_digest_that_is_not_a_sha256_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_lane([self.peer(0), self.peer(1)], app_apk_sha256="ABC")


class CommandLineTests(unittest.TestCase):
    def test_a_peer_declaration_carries_exactly_the_declared_fields(self):
        fields = cli._peer(
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json,identity=jlink:1050252028"
        )
        self.assertEqual(
            fields,
            {
                "port": "/dev/cu.usbmodem1",
                "contract": "/c.json",
                "profile": "/p.json",
                "identity": "jlink:1050252028",
            },
        )

    def test_a_malformed_peer_declaration_is_refused(self):
        import argparse

        for text in (
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json",
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json,identity=x,extra=1",
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json,identity=x,identity=y",
            "port=/dev/cu.usbmodem1,contract,profile=/p.json,identity=x",
            "port=/dev/cu.usbmodem1,contract=,profile=/p.json,identity=x",
        ):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                cli._peer(text)

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
            "--output",
            "/tmp/out",
            "--authorize-install",
            "--authorize-fixture-start",
            "--authorize-bluetooth-permissions",
        ]

    def peer_argument(self, index: int, *, port: str | None = None) -> str:
        return (
            f"port={port or f'/dev/cu.usbmodem{index}'},contract=/tmp/c.json,"
            f"profile=/tmp/p.json,identity=jlink:{1050252028 + index}"
        )

    def test_one_peer_is_refused_before_any_work(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main([*self.base_arguments(), "--peer", self.peer_argument(0)])
        self.assertEqual(raised.exception.code, 2)

    def test_a_shared_port_is_refused_before_any_work(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(
                [
                    *self.base_arguments(),
                    "--peer",
                    self.peer_argument(0, port="/dev/cu.usbmodem1"),
                    "--peer",
                    self.peer_argument(1, port="/dev/cu.usbmodem1"),
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_an_upper_case_digest_is_refused_before_any_work(self):
        arguments = self.base_arguments()
        arguments[arguments.index("0" * 64)] = "A" * 64
        with self.assertRaises(SystemExit) as raised:
            cli.main(
                [
                    *arguments,
                    "--peer",
                    self.peer_argument(0),
                    "--peer",
                    self.peer_argument(1),
                ]
            )
        self.assertEqual(raised.exception.code, 2)


class RoundArtifactTests(unittest.TestCase):
    """A repeated scene keeps its documented log name for one round, and names each otherwise."""

    def test_a_single_round_keeps_the_documented_file_name(self):
        self.assertEqual(
            lane._round_log(Path("/attempt"), 0, 1),
            Path("/attempt/instrumentation.log"),
        )

    def test_each_round_gets_its_own_log(self):
        self.assertEqual(
            lane._round_log(Path("/attempt"), 2, 3),
            Path("/attempt/instrumentation-2.log"),
        )


if __name__ == "__main__":
    unittest.main()
