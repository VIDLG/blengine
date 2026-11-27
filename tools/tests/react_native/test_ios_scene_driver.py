"""Unit contract for the in-process iOS scene driver lane.

The lane's value is that it is *small*: a declarative plan over the App's pinned machine
surface, one receipt returned as an attachment, and no WebDriver anywhere. These tests
hold that shape — if a future change adds a scroll step, a stdout receipt or a silently
different attachment lookup, it fails here instead of on a phone.
"""

from __future__ import annotations

import json
import subprocess
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.react_native import run_ios_scene_driver as lane
from tools.react_native import run_status
from tools.react_native.bleng_scan_evidence import SCENES

DEVICE = "00008110-000C61160C39401E"
# The ops that address exactly one element of the App's pinned surface, and so must name it.
# The rest are either environmental (`answerAlert`), a state to establish (`activate`), a
# platform window (`background`) or a disjunction whose elements live in its branches.
ELEMENT_OPS = {"wait", "still", "tap", "tapWhen", "tapUntil", "read", "setText"}
OPS = {
    "wait",
    "waitAny",
    "still",
    "tap",
    "tapWhen",
    "tapUntil",
    "read",
    "answerAlert",
    "activate",
    "background",
    "setText",
}
RECEIPT = "ios-scene-driver-receipt_0_7F6E1B00-0000-4000-8000-000000000000.json"


def _manifest(name: str = RECEIPT, exported: str = "ABC.json") -> str:
    return json.dumps(
        [
            {
                "testIdentifier": "BlengSceneDriverUITests/testScenePlan()",
                "attachments": [
                    {
                        "deviceId": DEVICE,
                        "exportedFileName": exported,
                        "suggestedHumanReadableName": name,
                        "isAssociatedWithFailure": False,
                    }
                ],
            }
        ]
    )


def _count(steps, op: str) -> int:
    return sum(1 for step in steps if step.get("op") == op)


def _count(steps, op: str) -> int:
    return sum(1 for step in steps if step.get("op") == op)


class PlanShapeTests(unittest.TestCase):
    def test_every_step_names_a_known_op(self) -> None:
        # Composed plans are what a device actually runs, so they are checked with the shipped
        # ones: a step shape that only exists at run time is still a step the drivers execute.
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            service_filter="fd600000-0000-4000-8000-000000000000",
            subscription={
                "uuid": "fd600000-0000-4000-8000-000000000001",
                "mode": "notification",
                "value": "ef",
            },
            gatt={"uuid": "fd600000-0000-4000-8000-000000000001", "write": "7"},
            scene="recovery",
            cycles=3,
        )
        for name, steps in {**lane.PLANS, **composed}.items():
            for step in steps:
                self.assertIn(step.get("op"), OPS, f"{name}: {step}")
                if step["op"] == "answerAlert":
                    # It names what it may answer and which button it may press; it never
                    # names an element of the App and never a coordinate.
                    self.assertNotIn("id", step, f"{name}: {step}")
                    rules = step.get("rules")
                    self.assertTrue(rules, f"{name}: {step}")
                    for rule in rules:
                        self.assertTrue(rule.get("match"), f"{name}: {step}")
                        self.assertTrue(rule.get("buttons"), f"{name}: {step}")
                        # A title is matched case-insensitively, so a localized prompt
                        # needs its own entry: one locale alone silently stops matching.
                        self.assertGreaterEqual(
                            len(rule["match"]), 2, f"{name}: {step}"
                        )
                elif step["op"] == "activate":
                    self.assertEqual(set(step), {"op"}, f"{name}: {step}")
                elif step["op"] == "background":
                    # Leaving the App is the one platform wait whose window cannot be a
                    # poll: the step states it, and both drivers refuse a step without one.
                    self.assertEqual(set(step), {"op", "for"}, f"{name}: {step}")
                    self.assertIsInstance(step["for"], int, f"{name}: {step}")
                    self.assertTrue(0 < step["for"] <= 60, f"{name}: {step}")
                elif step["op"] == "waitAny":
                    # A disjunction answers one question with several elements, so it names
                    # them in its branches rather than at the top of the step.
                    self.assertNotIn("id", step, f"{name}: {step}")
                    for branch in step["any"]:
                        self.assertRegex(
                            branch.get("id", ""),
                            r"^[a-z][a-z0-9-]+$",
                            f"{name}: {branch}",
                        )
                elif step["op"] in ELEMENT_OPS:
                    self.assertRegex(
                        step.get("id", ""), r"^[a-z][a-z0-9-]+$", f"{name}: {step}"
                    )
                    if step["op"] == "setText":
                        self.assertIsInstance(step.get("text"), str, f"{name}: {step}")

    def test_conditions_only_use_the_declared_keys(self) -> None:
        allowed = {"id", "exists", "label", "labelContains", "status", "enabled"}
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            subscription={
                "uuid": "fff20000-0000-4000-8000-000000000000",
                "mode": "notification",
                "value": "ef",
            },
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000", "write": "7"},
            scene="recovery",
            cycles=3,
        )
        for name, steps in {**lane.PLANS, **composed}.items():
            for step in steps:
                for key in ("when", "until"):
                    condition = step.get(key)
                    if condition is None:
                        continue
                    self.assertTrue(set(condition) <= allowed, f"{name}: {condition}")
                    self.assertIn("id", condition, f"{name}: {condition}")
                branches = step.get("any")
                if branches is None:
                    continue
                # A disjunction is 2..8 conditions, each of the same permitted shape.
                self.assertTrue(2 <= len(branches) <= 8, f"{name}: {branches}")
                for branch in branches:
                    self.assertTrue(set(branch) <= allowed, f"{name}: {branch}")
                    self.assertIn("id", branch, f"{name}: {branch}")

    def test_no_plan_scrolls_or_uses_coordinates(self) -> None:
        """Machine facts are pinned; a gesture-based plan would be platform-specific.

        This is a design guard, not a style rule: Android's accessibility tree omits
        off-viewport `ScrollView` children, so any scene that needs a swipe behaves
        differently per platform and only as accurately as input injection.
        """
        forbidden = {"swipe", "scroll", "scrollTo", "drag", "coordinate"}
        for name, steps in lane.PLANS.items():
            for step in steps:
                self.assertNotIn(step.get("op"), forbidden, f"{name}: {step}")
                self.assertFalse(forbidden & set(step), f"{name}: {step}")

    def test_a_fresh_install_declares_every_environmental_prompt(self) -> None:
        fresh = lane.plans(fresh_install=True, attach=True)
        # launch prompt, post-activate prompt, post-start-session prompt; the scan plan
        # adds the one the first scan raises on top of the use-Bluetooth consent.
        self.assertEqual(_count(fresh["smoke"], "answerAlert"), 3)
        self.assertEqual(_count(fresh["scan"], "answerAlert"), 4)
        for name in ("smoke", "scan"):
            self.assertEqual(_count(lane.PLANS[name], "answerAlert"), 0, name)
            # Neither may be `required`: an already-authorized App shows no prompt.
            self.assertFalse(
                any(
                    step.get("required")
                    for step in fresh[name]
                    if step.get("op") == "answerAlert"
                ),
                name,
            )
            # Every optional prompt step declares how long it watches for one. Without that
            # the driver spent the whole 4 s timeout on a state the plan did not expect, which
            # measured as about a quarter of every scene's wall clock on a device.
            graces = [
                step.get("grace")
                for step in fresh[name]
                if step.get("op") == "answerAlert"
            ]
            self.assertNotIn(None, graces, name)
            self.assertTrue(
                all(isinstance(g, (int, float)) and 0 <= g <= 4.0 for g in graces),
                graces,
            )
            self.assertGreaterEqual(
                max(graces),
                2.0,
                f"{name}: the post-action prompt sites need a real window",
            )

    def test_every_composed_prompt_step_states_its_window(self) -> None:
        # `grace` is part of the shared vocabulary, not an iOS convenience: the iOS executor
        # watches an optional prompt for it, and the Android executor watches the same window
        # (after one cheap question about which package is frontmost). A scene that composes
        # its own steps must state it too, or one platform would still wait out the timeout.
        composed = lane.plans(
            fresh_install=True,
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            service_filter="fd600000-0000-4000-8000-000000000000",
            subscription={
                "uuid": "fd600000-0000-4000-8000-000000000001",
                "mode": "notification",
                "value": "ef",
            },
            gatt={"uuid": "fd600000-0000-4000-8000-000000000001", "write": "7"},
        )
        prompts = 0
        for name, steps in composed.items():
            for step in steps:
                if step.get("op") != "answerAlert":
                    continue
                prompts += 1
                with self.subTest(plan=name):
                    grace = step.get("grace")
                    self.assertIsInstance(grace, (int, float), f"{name}: {step}")
                    self.assertGreaterEqual(grace, 0, name)
                    self.assertLessEqual(grace, step.get("timeout", 0), name)
        self.assertGreater(prompts, 0, "no prompt step was composed to check")

    def test_the_background_plan_leaves_the_app_and_returns_to_it(self) -> None:
        # The scene's whole claim is an ordering: subscribe, receive a value, leave the App,
        # let the host emit while it is away, come back, and receive *that* value. Each part
        # is its own phase, so a plan that quietly stayed in the foreground cannot pass.
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            service_filter="fd600000-0000-4000-8000-000000000000",
            subscription={
                "uuid": "fd600000-0000-4000-8000-000000000001",
                "mode": "notification",
                "value": "ef",
            },
        )
        away = composed["background.away"]
        self.assertEqual([step.get("op") for step in away], ["background"])
        # An instant of being away is not a background window: the step states how long the
        # App must stay away, and the scene's own budget covers it.
        self.assertIn("for", away[0], away)
        self.assertGreaterEqual(
            away[0]["for"],
            10,
            "a window shorter than iOS takes to suspend proves nothing",
        )
        self.assertGreaterEqual(SCENES["background"].min_seconds, away[0]["for"])
        back = composed["background.back"]
        ops = [step.get("op") for step in back]
        self.assertEqual(
            ops[:2], ["activate", "wait"], "returning is proven, not assumed"
        )
        self.assertLess(ops.index("activate"), ops.index("tap"), "drain after resuming")
        self.assertIn("close-session", [step.get("id") for step in back])
        # The value that proves the subscription outlived the cycle is the one emitted while
        # the App was away, so the pull has to come after the resume, never before it.
        self.assertEqual(
            [step.get("as") for step in back if step.get("op") == "read"],
            ["resumed", "notified", "drained", "ledger", "closed"],
        )
        self.assertEqual(
            [step.get("op") for step in composed["background.value"]][:2],
            ["activate", "wait"],
        )

    def test_pull_loops_are_bounded(self) -> None:
        for name, steps in lane.PLANS.items():
            for step in steps:
                if step.get("op") != "tapUntil":
                    continue
                self.assertGreaterEqual(step.get("maxTaps", 0), 1, f"{name}: {step}")
                self.assertLessEqual(step.get("maxTaps", 0), 64, f"{name}: {step}")
                self.assertIn("timeout", step, f"{name}: {step}")

    def test_every_plan_ends_the_session_it_started(self) -> None:
        # `probe` is deliberately not a scene: it drives nothing and is the one plan that
        # may leave the device as it found it.
        for name in ("smoke", "scan"):
            ids = [step.get("id") for step in lane.PLANS[name]]
            self.assertEqual(ids.count("start-session"), 1, name)
            self.assertEqual(ids.count("close-session"), 1, name)
            self.assertLess(
                ids.index("start-session"), ids.index("close-session"), name
            )
            self.assertLess(
                ids.index("start-session"), ids.index("close-session"), name
            )

    def test_scan_plan_asserts_the_projection_not_rendered_content(self) -> None:
        ids = [step.get("id") for step in lane.SCAN_PLAN]
        self.assertIn(lane.STATUS_ID, ids)
        # Sample/ledger text lives in scrollable content; a scene may not read it.
        self.assertNotIn("scan-samples", ids)
        self.assertNotIn("lifecycle-evidence", ids)

    def test_the_write_reject_plan_parks_a_write_and_reads_around_it(self) -> None:
        steps = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000", "write": "7"},
        )
        opened = steps["write-reject.open"]
        dispatch = steps["write-reject.dispatch"]
        settle = steps["write-reject.settle"]
        # The payload is typed and the value is read *before* the peer parks anything: a
        # hold window must not contain UI work, and the initial read is the baseline.
        self.assertLess(
            [step.get("id") for step in opened].index("gatt-read"),
            [step.get("id") for step in opened].index("gatt-write-value"),
        )
        # Exactly one submission per phase, and the dispatch phase proves the App accepted
        # it and is still waiting (nothing else is published while a write waits).
        self.assertEqual([step.get("id") for step in dispatch].count("gatt-write"), 1)
        ledger_steps = [step for step in dispatch if step.get("id") == "action-ledger"]
        self.assertEqual([step.get("op") for step in ledger_steps], ["wait", "read"])
        self.assertEqual(ledger_steps[0]["labelContains"], "gatt-write-wait|")
        ids = [step.get("id") for step in settle]
        self.assertEqual(ids.count("gatt-read"), 2)
        self.assertEqual(ids.count("gatt-write"), 1)
        # The refusal is read as the pinned error row, and a refused write renders nothing.
        self.assertLess(ids.index("gatt-error"), ids.index("gatt-result"))
        self.assertIn(
            {"op": "wait", "id": "gatt-result", "exists": False, "timeout": 10},
            settle,
        )
        self.assertEqual(ids.count("close-session"), 1)

    def test_every_ledger_read_states_what_it_expects_to_see(self) -> None:
        # A tap only injects a touch: on UiAutomator the read that follows can win the race
        # against the App's own handler and describe the ledger from before it. Observed on a
        # device as a `ccc-disable-reject` failure whose ledger stopped at the previous
        # phase. Every ledger read therefore follows a step that waits for the fact it is
        # about to be interpreted as.
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            service_filter="fd600000-0000-4000-8000-000000000000",
            subscription={
                "uuid": "fd600000-0000-4000-8000-000000000001",
                "mode": "notification",
                "value": "ef",
            },
            gatt={"uuid": "fd600000-0000-4000-8000-000000000001", "write": "7"},
        )
        settled = {"wait", "waitAny", "still"}
        reads = 0
        for name, steps in composed.items():
            for index, step in enumerate(steps):
                if step.get("op") != "read" or step.get("id") != "action-ledger":
                    continue
                reads += 1
                with self.subTest(plan=name, position=index):
                    self.assertGreater(index, 0, "a ledger read needs a preceding step")
                    self.assertIn(steps[index - 1].get("op"), settled)
        self.assertGreater(reads, 0, "no ledger read was found to check")

    def test_the_ccc_plans_park_a_ccc_write_and_probe_one_disjunction(self) -> None:
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            subscription={
                "uuid": "fff20000-0000-4000-8000-000000000000",
                "mode": "notification",
                "value": "ef",
            },
        )
        enable = composed["ccc-enable-reject.enable"]
        self.assertEqual([step.get("id") for step in enable].count("gatt-subscribe"), 1)
        # The witness is the App's own ledger row, not the transient status: a device whose
        # automation channel stalls for two minutes reads the status after it has settled
        # (measured 2026-11-26, one press at 120.6 s), while `pending` on the wait row is the
        # fact the scene actually claims.
        self.assertIn(
            {
                "op": "wait",
                "id": "action-ledger",
                "labelContains": "notify-enable-wait|",
                "timeout": 30,
            },
            enable,
        )
        self.assertNotIn(
            {"notify": "enabling"},
            [step.get("status") for step in enable if step.get("op") == "wait"],
        )
        disable = composed["ccc-disable-reject.probe"]
        disjunctions = [step for step in disable if step.get("op") == "waitAny"]
        self.assertEqual(len(disjunctions), 1)
        branches = disjunctions[0]["any"]
        ids = [branch["id"] for branch in branches]
        # One branch is the App's own phase (delivered), the other its own record (no value).
        self.assertEqual(ids, [lane.STATUS_ID, "action-ledger"])
        self.assertIn("timeout", disjunctions[0])
        # A refused disable must never be read as an end before the probe has run.
        probes = [step.get("id") for step in disable]
        self.assertLess(
            probes.index("pull-notification"), probes.index("disable-notifications")
        )

    def test_the_passive_plan_measures_a_quiet_window_and_withdraws_controls(
        self,
    ) -> None:
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000"},
        )
        opened = composed["passive-disconnect.open"]
        ended = composed["passive-disconnect.ended"]
        self.assertEqual(
            [step.get("id") for step in ended if step.get("op") == "tap"],
            ["close-session"],
        )
        quiet = [step for step in ended if step.get("op") == "still"]
        self.assertEqual(len(quiet), 1)
        self.assertEqual(quiet[0]["id"], lane.STATUS_ID)
        self.assertGreaterEqual(quiet[0]["for"], 5)
        self.assertLessEqual(quiet[0]["for"], 30)
        withdrawn = [
            step["id"]
            for step in ended
            if step.get("op") == "wait" and step.get("enabled") is False
        ]
        self.assertEqual(sorted(withdrawn), sorted(lane.CONTROLS_REQUIRING_LINK))
        # The link has to work before its end means anything.
        self.assertIn("gatt-read", [step.get("id") for step in opened])

    def test_the_recovery_plans_repeat_the_cycle_on_one_owner(self) -> None:
        composed = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000"},
            scene="recovery",
            cycles=3,
        )
        self.assertIn(
            "start-session", [s.get("id") for s in composed["recovery.scan:0"]]
        )
        # The target input belongs to the App: only the first cycle types it, and the later
        # cycles still wait for it to resolve to exactly one row.
        self.assertIn("gatt-target", [s.get("id") for s in composed["recovery.scan:0"]])
        for cycle in (1, 2):
            ids = [s.get("id") for s in composed[f"recovery.scan:{cycle}"]]
            self.assertNotIn("start-session", ids)
            self.assertNotIn("gatt-target", ids)
            self.assertIn("start-scan", ids)
        # Only the last cycle closes the session it started.
        self.assertIn(
            "close-session",
            [s.get("id") for s in composed["recovery.announce:2"]],
        )
        self.assertNotIn(
            "close-session",
            [s.get("id") for s in composed["recovery.announce:1"]],
        )

    def test_every_plan_names_its_reads_uniquely(self) -> None:
        """A receipt is one flat `as` -> text map, so a repeated name silently overwrites.

        This is not hypothetical: the io plan first reused `initial` for its first GATT
        value, which is the session precondition's name, and the receipt then decoded a
        characteristic rendering as a `run-status` frame on the device.
        """
        composed = lane.plans(
            fresh_install=True,
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            subscription={
                "uuid": "fff20000-0000-4000-8000-000000000000",
                "mode": "notification",
                "value": "ef",
            },
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000", "write": "7"},
            scene="recovery",
            cycles=3,
        )
        for name, steps in {**lane.PLANS, **composed}.items():
            names = [step["as"] for step in steps if step.get("op") == "read"]
            self.assertEqual(len(names), len(set(names)), f"{name}: {names}")

    def test_the_io_plan_holds_to_the_same_shape(self) -> None:
        steps = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000", "write": "7"},
        )["io"]
        ids = [step.get("id") for step in steps]
        for step in steps:
            self.assertIn(step.get("op"), OPS, step)
        self.assertEqual(ids.count("start-session"), 1)
        self.assertEqual(ids.count("close-session"), 1)
        self.assertLess(ids.index("start-session"), ids.index("close-session"))
        # It acts on the pinned command surface only: the outcome rows and the inputs are
        # rendered outside every scroll container, so no step names scrollable content.
        self.assertNotIn("scan-samples", ids)
        self.assertNotIn("lifecycle-evidence", ids)
        self.assertIn("gatt-write-value", ids)
        self.assertIn("rssi-value", ids)
        self.assertEqual(ids.count("gatt-read"), 2)
        self.assertEqual(ids.count("gatt-write"), 1)

    def test_the_io_plan_tells_two_reads_apart_by_what_is_rendered(self) -> None:
        """A read settles into a *value*, and the App clears the row when it dispatches.

        Waiting for the phase would pass on the first read's `done`; waiting for mere
        presence would pass on the write receipt. Only the value rendering distinguishes the
        read the scene just asked for from both of those.
        """
        steps = lane.plans(
            attach=True,
            expected_local_name="BleHubMultiRfV2DKA",
            gatt={"uuid": "fff10000-0000-4000-8000-000000000000", "write": "7"},
        )["io"]
        waits = [
            step
            for step in steps
            if step.get("op") == "wait" and step.get("id") == "gatt-result"
        ]
        self.assertEqual(len(waits), 3)
        renderings = [step.get("labelContains") for step in waits]
        self.assertEqual(
            renderings,
            [lane.VALUE_RENDERING, lane.WRITE_RENDERING, lane.VALUE_RENDERING],
        )
        # A value rendering is never a substring of a write receipt, which is what makes the
        # second read unambiguous without asserting any particular byte value.
        self.assertNotIn(lane.VALUE_RENDERING, "fff1: wrote 1 bytes (withResponse)")
        self.assertNotIn(lane.WRITE_RENDERING, "fff1: 1 bytes [00]")


class ReceiptExportTests(unittest.TestCase):
    def _export(
        self,
        *,
        manifest: str | None,
        payload: dict | None = None,
        returncode: int = 0,
    ) -> dict:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "scene.xcresult"
            bundle.mkdir()
            destination = root / "attachments"

            def run(command, *, cwd, timeout, check):
                self.assertEqual(command[:3], [lane.XCRUN, "xcresulttool", "export"])
                destination.mkdir()
                if manifest is not None:
                    (destination / "manifest.json").write_text(
                        manifest, encoding="utf-8"
                    )
                    # Mirror the real export: every listed attachment becomes a file.
                    for group in json.loads(manifest):
                        for entry in group.get("attachments") or []:
                            name = entry.get("exportedFileName")
                            if Path(name).name != name:
                                continue
                            body = (
                                json.dumps(payload)
                                if payload is not None
                                and lane._RECEIPT_FILE.fullmatch(
                                    str(entry.get("suggestedHumanReadableName"))
                                )
                                else "hierarchy"
                            )
                            (destination / name).write_text(body, encoding="utf-8")
                return subprocess.CompletedProcess(command, returncode, "")

            with patch.object(lane.ios_sdk, "run_command", side_effect=run):
                export = lane._export_attachments(
                    bundle, destination, cwd=root, timeout=10
                )
                return (
                    lane._read_receipt(destination, export),
                    list(export.diagnostics),
                )

    def test_decodes_the_one_named_receipt(self) -> None:
        payload = {"scope": "ios-scene-driver", "status": "passed", "steps": []}
        receipt, diagnostics = self._export(manifest=_manifest(), payload=payload)
        self.assertEqual(receipt, payload)
        self.assertEqual(diagnostics, [])

    def test_diagnostics_are_listed_not_mistaken_for_the_receipt(self) -> None:
        manifest = json.loads(_manifest())
        manifest[0]["attachments"].append(
            {
                "exportedFileName": "DEF.txt",
                "suggestedHumanReadableName": "ios-scene-driver-app-hierarchy_0_AB.json",
            }
        )
        payload = {"scope": "ios-scene-driver", "status": "failed", "steps": []}
        receipt, diagnostics = self._export(
            manifest=json.dumps(manifest), payload=payload
        )
        self.assertEqual(receipt, payload)
        self.assertEqual(
            [row["file"] for row in diagnostics],
            ["DEF.txt"],
        )
        self.assertGreater(diagnostics[0]["bytes"], 0)

    def test_the_receipt_name_carries_a_uniqueness_suffix(self) -> None:
        """`xcresulttool` never exports the declared name verbatim."""
        with self.assertRaisesRegex(RuntimeError, "expected exactly one"):
            self._export(
                manifest=_manifest(name="ios-scene-driver-receipt.json"),
                payload={"scope": "ios-scene-driver"},
            )

    def test_a_foreign_scope_is_not_a_receipt(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not this driver's scope"):
            self._export(manifest=_manifest(), payload={"scope": "other"})

    def test_two_receipts_are_ambiguous(self) -> None:
        manifest = json.loads(_manifest())
        manifest.append(
            {
                "testIdentifier": "other()",
                "attachments": [
                    {
                        "exportedFileName": "ABC.json",
                        "suggestedHumanReadableName": RECEIPT,
                    }
                ],
            }
        )
        with self.assertRaisesRegex(RuntimeError, "expected exactly one"):
            self._export(manifest=json.dumps(manifest), payload={})

    def test_an_unsafe_exported_name_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsafe exported attachment name"):
            self._export(manifest=_manifest(exported="../../etc/passwd"), payload={})

    def test_a_failed_export_names_the_exit(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "could not export"):
            self._export(manifest=None, payload=None, returncode=64)

    def test_a_missing_manifest_is_not_an_empty_receipt(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no manifest"):
            self._export(manifest=None, payload={})

    def test_an_unusable_receipt_still_leaves_what_was_exported_listed(self) -> None:
        # The failure that matters most to a reader is the one whose receipt is missing,
        # ambiguous or foreign. The export is a separate step exactly so the hierarchy it
        # did produce survives the refusal instead of dying with the attempt directory.
        manifest = json.loads(_manifest())
        manifest[0]["attachments"] = [
            row
            for row in manifest[0]["attachments"]
            if row["suggestedHumanReadableName"] != RECEIPT
        ]
        manifest[0]["attachments"].append(
            {
                "exportedFileName": "HIER.txt",
                "suggestedHumanReadableName": "ios-scene-driver-app-hierarchy_0_AB",
            }
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "scene.xcresult"
            bundle.mkdir()
            destination = root / "attachments"

            def run(command, *, cwd, timeout, check):
                destination.mkdir()
                (destination / "manifest.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                (destination / "HIER.txt").write_text("hierarchy", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "")

            with patch.object(lane.ios_sdk, "run_command", side_effect=run):
                export = lane._export_attachments(
                    bundle, destination, cwd=root, timeout=10
                )
                self.assertEqual(
                    [row["file"] for row in export.diagnostics], ["HIER.txt"]
                )
                self.assertEqual(export.receipts, ())
                with self.assertRaisesRegex(RuntimeError, "expected exactly one"):
                    lane._read_receipt(destination, export)


class RunSceneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.xctestrun = self.root / "host.xctestrun"
        self.xctestrun.write_text("<plist/>", encoding="utf-8")
        self.run_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _run(
        self,
        receipt: dict,
        *,
        xcodebuild_returncode: int = 0,
        xcodebuild_output: str = "",
        **overrides,
    ) -> Path:
        target = type("Target", (), {"name": "BleHubBlengHostUITests"})()

        def run(command, *, cwd, timeout, check):
            if "-resultBundlePath" in command:
                Path(command[command.index("-resultBundlePath") + 1]).mkdir()
                return subprocess.CompletedProcess(
                    command, xcodebuild_returncode, xcodebuild_output
                )
            destination = Path(command[command.index("--output-path") + 1])
            destination.mkdir()
            (destination / "manifest.json").write_text(_manifest(), encoding="utf-8")
            (destination / "ABC.json").write_text(json.dumps(receipt), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "")

        options = {
            "device": DEVICE,
            "xctestrun": self.xctestrun,
            "plan_name": "smoke",
            "output_root": self.root / "out",
            "timeout": 120,
            "launch": True,
            "cwd": self.root,
            "run_id": self.run_id,
        }
        options.update(overrides)
        with (
            patch.object(lane, "resolve_xctestrun", return_value=target),
            patch.object(lane, "staged_xctestrun", return_value=self.xctestrun),
            patch.object(lane.ios_sdk, "run_command", side_effect=run),
        ):
            return lane.run_scene(**options)

    def _frames(self) -> dict[str, str]:
        """One valid projection frame per `run-status` read the plan declares."""
        frame = ";".join(f"{key}=none" for key in run_status.KEYS)
        return {
            step["as"]: frame
            for step in lane.SMOKE_PLAN
            if step.get("op") == "read" and step.get("id") == lane.STATUS_ID
        }

    def _receipt(self, status: str) -> dict:
        return {
            "scope": "ios-scene-driver",
            "status": status,
            "primary_error": "" if status == "passed" else "scene step 3 failed",
            "steps": [
                {
                    "index": index,
                    "step": {"op": "wait", "id": "run-status"},
                    "seconds": 0.25,
                }
                for index in range(len(lane.SMOKE_PLAN))
            ],
            "values": self._frames(),
        }

    def test_a_passed_scene_publishes_its_receipt_and_timings(self) -> None:
        path = self._run(self._receipt("passed"))
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scope"], lane.SCOPE)
        self.assertEqual(result["plan"], "smoke")
        self.assertAlmostEqual(
            result["step_seconds_total"], 0.25 * len(lane.SMOKE_PLAN), places=3
        )
        self.assertEqual(result["receipt"]["status"], "passed")
        # Every `run-status` read is decoded strictly and published, so the host audits
        # the same facts the driver matched against instead of trusting a label.
        self.assertEqual(set(result["projection"]), set(self._frames()))
        self.assertEqual(set(result["projection"]["closed"]), set(run_status.KEYS))

    def test_a_drifted_projection_fails_the_scene(self) -> None:
        receipt = self._receipt("passed")
        name = min(receipt["values"])
        receipt["values"][name] = "state=started"
        with self.assertRaisesRegex(RuntimeError, "not this contract"):
            self._run(receipt)

    def test_a_missing_projection_read_fails_the_scene(self) -> None:
        receipt = self._receipt("passed")
        receipt["values"].pop(min(receipt["values"]))
        with self.assertRaisesRegex(RuntimeError, "has no"):
            self._run(receipt)

    def test_a_failed_scene_still_publishes_before_raising(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "scene plan failed"):
            self._run(self._receipt("failed"))
        failed = self.root / "out" / self.run_id / "result.json"
        result = json.loads(failed.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("scene plan failed", result["primary_error"])

    def test_a_nonzero_xcodebuild_overrides_a_passing_receipt(self) -> None:
        """XCTest can abort without throwing; the receipt alone is not the verdict."""
        with self.assertRaisesRegex(RuntimeError, "xcodebuild failed"):
            self._run(self._receipt("passed"), xcodebuild_returncode=65)
        published = self.root / "out" / self.run_id / "result.json"
        result = json.loads(published.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["xcodebuild_returncode"], 65)
        self.assertEqual(result["receipt"]["status"], "passed")

    def test_only_that_failure_is_translated(self) -> None:
        for output in (
            "some other failure",
            "The test runner failed to initialize for UI testing. (Underlying Error: Device locked.)",
            "Scene step failed: Timed out while enabling automation mode.",
        ):
            with self.subTest(output=output):
                self.assertIsNone(
                    lane.runner_initialization_error(output, returncode=65)
                )

    def test_a_failed_result_keeps_xcodebuild_s_own_words(self) -> None:
        # A managed scene discards the raw attempt directory; without this tail the result
        # says only that a receipt is missing.
        with self.assertRaises(RuntimeError):
            self._run(
                self._receipt("failed"),
                xcodebuild_returncode=65,
                xcodebuild_output="** TEST EXECUTE FAILED **\n",
            )
        result = json.loads(
            (self.root / "out" / self.run_id / "result.json").read_text()
        )
        self.assertIn("TEST EXECUTE FAILED", result["xcodebuild_output"])

    def test_a_passed_result_carries_no_console_tail(self) -> None:
        result = json.loads(self._run(self._receipt("passed")).read_text())
        self.assertIsNone(result["xcodebuild_output"])

    def test_a_slow_passed_phase_keeps_xcodebuild_s_own_words(self) -> None:
        # A phase of many slow presses says what it cost but not why; XCTest's warnings about
        # idle/animations only exist in this log, so a slow phase carries it even when it passed.
        receipt = self._receipt("passed")
        for step in receipt["steps"]:
            step["seconds"] = 120.5
        result = json.loads(
            self._run(
                receipt,
                xcodebuild_output="App animations complete notification not received",
            ).read_text()
        )
        self.assertEqual(result["status"], "passed")
        self.assertIn("animations complete", result["xcodebuild_output"])

    def test_a_short_receipt_is_rejected(self) -> None:
        receipt = self._receipt("passed")
        receipt["steps"] = receipt["steps"][:-1]
        with self.assertRaisesRegex(RuntimeError, "must report all"):
            self._run(receipt)

    def test_a_failed_receipt_prefix_is_kept_as_evidence(self) -> None:
        """A scene that stopped early still reports the step it stopped at."""
        receipt = self._receipt("failed")
        receipt["primary_error"] = "step 3 timed out"
        receipt["steps"] = receipt["steps"][:4]
        with self.assertRaisesRegex(RuntimeError, "scene plan failed"):
            self._run(receipt)
        result = json.loads(
            (self.root / "out" / self.run_id / "result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(result["receipt"]["steps"]), 4)
        self.assertIn("step 3 timed out", result["primary_error"])

    def test_launch_and_attach_are_different_plan_shapes(self) -> None:
        """Attach mode must foreground the App the host pinned; launch mode must not."""
        launch = lane.plans()
        attach = lane.plans(attach=True)
        for plan_name in ("smoke", "scan"):
            ops = [step.get("op") for step in launch[plan_name]]
            self.assertNotIn("activate", ops, plan_name)
            self.assertEqual(ops.count("activate"), 0, plan_name)
            attached = [step.get("op") for step in attach[plan_name]]
            self.assertEqual(attached.count("activate"), 1, plan_name)
            # Foregrounding is the first thing the plan does, before any App element is
            # read: a prompt must be answered before activation can succeed.
            self.assertLess(
                attached.index("activate"), attached.index("wait"), plan_name
            )

    def test_an_unknown_plan_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown scene plan"):
            self._run(self._receipt("passed"), plan_name="nope")

    def test_an_unbounded_timeout_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most"):
            self._run(self._receipt("passed"), timeout=lane.TIMEOUT_MAX + 1)

    def test_a_missing_xctestrun_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing xctestrun"):
            self._run(self._receipt("passed"), xctestrun=self.root / "absent.xctestrun")


if __name__ == "__main__":
    unittest.main()
