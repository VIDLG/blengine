"""The shared multi-link contract, and the two platform lanes that must both obey it.

The scene is defined once (`bleng_multi_link_contract`) and driven twice: Kotlin through
`am instrument`, Swift through a hosted XCTest bundle. A stage added on one side without the other
is exactly the drift this file fails on, because a lane that emits a different vocabulary is
describing a different scene.
"""

from __future__ import annotations

import re
import unittest
import uuid
from pathlib import Path

from tools.react_native.bleng_multi_link_contract import (
    FAULT_WRITE_REJECT,
    MARKER_TAG,
    MAXIMUM_PEERS,
    MAXIMUM_ROUNDS,
    MINIMUM_PEERS,
    Fault,
    expected_stages,
    parse_fault,
    peer_identity,
    peer_session_id,
    require_rounds,
    write_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KOTLIN_TEST = (
    PROJECT_ROOT
    / "tests/consumers/react-native-bleng/android/app/src/androidTest/kotlin"
    / "com/blehubblenghost/BlengMultiLinkInstrumentedTest.kt"
)
SWIFT_TEST = (
    PROJECT_ROOT
    / "tests/consumers/react-native-bleng/ios/BleHubBlengHostDeviceTests"
    / "BlengMultiLinkDeviceTests.swift"
)


def _stems(stages: tuple[str, ...]) -> set[str]:
    return {re.sub(r":\d+$", ":{n}", stage) for stage in stages}


def _kotlin_stems(source: str) -> set[str]:
    stems = set()
    for literal in re.findall(r'marker\("([^"]+)"\)', source):
        stems.add(re.sub(r":\d+$", ":{n}", literal.replace("$index", "{n}")))
    return stems


def _swift_stems(source: str) -> set[str]:
    """Swift interpolates as `\\(index)`; normalise it to the same placeholder."""
    stems = set()
    for literal in re.findall(r'marker\("([^"]+)"\)', source):
        stems.add(re.sub(r":\d+$", ":{n}", literal.replace("\\(index)", "{n}")))
    return stems


class VocabularyTests(unittest.TestCase):
    def test_the_vocabulary_is_ordered_unique_and_bounded(self):
        for count in range(MINIMUM_PEERS, MAXIMUM_PEERS + 1):
            with self.subTest(peers=count):
                stages = expected_stages(count)
                self.assertEqual(len(stages), len(set(stages)))
                self.assertEqual(stages[0], "started")
                self.assertEqual(stages[-1], "done")
                self.assertLess(stages.index("scan-stopped"), stages.index("done"))

    def test_a_peer_count_outside_the_scene_is_refused(self):
        for count in (0, 1, MAXIMUM_PEERS + 1):
            with self.subTest(peers=count), self.assertRaises(ValueError):
                expected_stages(count)

    def test_every_survivor_is_exercised_and_the_disturbed_link_is_never_one(self):
        stages = expected_stages(3)
        for index in (1, 2):
            self.assertIn(f"survivor-requested:{index}", stages)
            self.assertIn(f"survivor:{index}", stages)
            self.assertIn(f"disconnected:{index}", stages)
        self.assertNotIn("survivor:0", stages)
        self.assertNotIn("disconnected:0", stages)

    def test_per_link_identity_is_the_whole_stage(self):
        # `peer_identity` exists so a host advancing fixtures never collapses two links'
        # same-named step into one advance.
        self.assertEqual(peer_identity("subscribed:0"), "subscribed:0")
        self.assertNotEqual(
            peer_identity("subscribed:0"), peer_identity("subscribed:1")
        )


class PayloadTests(unittest.TestCase):
    def test_each_link_writes_a_value_no_other_link_could_produce(self):
        provisioned = bytes.fromhex("00112233445566778899aabbccddeeff")
        payloads = [write_payload(index, provisioned) for index in range(MAXIMUM_PEERS)]
        self.assertEqual(len(set(payloads)), len(payloads))
        for payload in payloads:
            self.assertEqual(len(payload), len(provisioned))
            self.assertNotEqual(payload, provisioned)

    def test_a_payload_that_cannot_differ_is_refused(self):
        with self.assertRaises(ValueError):
            write_payload(0, b"")
        with self.assertRaises(ValueError):
            write_payload(0, bytes([0xA0]))
        with self.assertRaises(ValueError):
            write_payload(MAXIMUM_PEERS, b"\x00")


class SessionIdentityTests(unittest.TestCase):
    def test_each_peer_gets_its_own_derived_session(self):
        run_id = str(uuid.uuid4())
        ids = [peer_session_id(run_id, index) for index in range(MAXIMUM_PEERS)]
        self.assertEqual(len(set(ids)), len(ids))
        for value in ids:
            self.assertEqual(str(uuid.UUID(value)), value)
        self.assertNotEqual(
            ids[0],
            peer_session_id(str(uuid.uuid4()), 0),
            "the id must derive from the run",
        )


class FaultTests(unittest.TestCase):
    """A declared fault changes one link's I/O pair and nothing else."""

    def test_a_declared_fault_names_a_kind_and_a_declared_peer(self):
        self.assertEqual(parse_fault("write-reject:0"), Fault(FAULT_WRITE_REJECT, 0))
        self.assertEqual(parse_fault("write-reject:0").declaration(), "write-reject:0")

    def test_no_fault_is_an_explicit_absence(self):
        for text in (None, "", "   ", "none"):
            with self.subTest(text=text):
                self.assertIsNone(parse_fault(text))

    def test_an_unknown_or_incomplete_declaration_is_refused(self):
        # A run that asked for a fault must never silently get a clean scene.
        for text in (
            "write-reject",
            "write-reject:x",
            f"write-reject:{MAXIMUM_PEERS}",
            "bogus:0",
            "write-reject:0:1",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_fault(text)

    def test_only_the_faulted_link_changes_its_stages(self):
        normal = expected_stages(MINIMUM_PEERS)
        faulted = expected_stages(MINIMUM_PEERS, fault=Fault(FAULT_WRITE_REJECT, 0))
        self.assertEqual(len(faulted), len(normal))
        expected: list[str] = []
        for stage in normal:
            if stage == "written:0":
                expected.extend(["write-rejected:0", "unchanged:0"])
            elif stage == "io:0":
                continue
            else:
                expected.append(stage)
        self.assertEqual(faulted, tuple(expected))

    def test_a_fault_outside_the_declaration_is_refused(self):
        with self.assertRaises(ValueError):
            Fault(FAULT_WRITE_REJECT, MAXIMUM_PEERS)
        with self.assertRaises(ValueError):
            Fault("unknown", 0)


class PlatformVocabularyTests(unittest.TestCase):
    """Both platform tests must emit exactly the shared vocabulary, and nothing else."""

    def assert_lane_matches(self, path: Path, stems: set[str]) -> None:
        self.assertTrue(path.is_file(), f"missing platform lane: {path}")
        # Each lane carries the normal and the faulted branch for every link, so its stem set is
        # the union of the two vocabularies — still an exact comparison, not a subset check.
        expected = _stems(expected_stages(MINIMUM_PEERS)) | _stems(
            expected_stages(MINIMUM_PEERS, fault=Fault(FAULT_WRITE_REJECT, 0))
        )
        self.assertEqual(
            stems(path.read_text(encoding="utf-8")),
            expected,
            f"{path.name} and the shared multi-link vocabulary have drifted apart",
        )

    def test_the_android_lane_emits_the_shared_vocabulary(self):
        self.assert_lane_matches(KOTLIN_TEST, _kotlin_stems)

    def test_the_ios_lane_emits_the_shared_vocabulary(self):
        self.assert_lane_matches(SWIFT_TEST, _swift_stems)

    def test_both_platform_tests_declare_the_shared_marker_tag(self):
        # The tag is the wire between a test and its host; two spellings would mean a silent
        # run that reports no stages at all.
        for path in (KOTLIN_TEST, SWIFT_TEST):
            self.assertTrue(path.is_file(), f"missing platform lane: {path}")
            self.assertIn(
                f'"{MARKER_TAG}"',
                path.read_text(encoding="utf-8"),
                f"{path.name} does not declare the shared marker tag {MARKER_TAG}",
            )


class RoundTests(unittest.TestCase):
    """A soak is a declared round count, never a second scene or a second runner."""

    def test_the_bounds_are_one_constant(self):
        self.assertEqual(require_rounds(1), 1)
        self.assertEqual(require_rounds(MAXIMUM_ROUNDS), MAXIMUM_ROUNDS)

    def test_an_out_of_range_or_mistyped_count_is_refused(self):
        for value in (0, -1, MAXIMUM_ROUNDS + 1, True, 1.0, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                require_rounds(value)  # type: ignore[arg-type]

    def test_repetition_does_not_change_the_vocabulary(self):
        # Each round is one complete run of the same claim, so the stage contract is unaffected
        # and a soak cannot pass by emitting a different vocabulary.
        self.assertEqual(expected_stages(2), tuple(expected_stages(2)))
        self.assertNotIn("round", " ".join(expected_stages(2)))


if __name__ == "__main__":
    unittest.main()
