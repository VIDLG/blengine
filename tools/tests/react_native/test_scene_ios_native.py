"""Contract for the in-process driver as an *envelope* driver.

The device run of this lane is a separate, human-gated step, so the rules that matter here are
the executor's own: what must reach the bundle, what must stay out of it, how a failed scene
still explains itself, and that each phase is one attach run against the same App.

What a scene *means* — its phases, ledgers, slices and observation derivations — lives in
`scene_native` and is covered executor-free in `test_scene_semantics.py`. A derivation test here
would be a second copy under a slower harness, so this module carries none.
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.react_native import run_status
from tools.react_native import scene_ios_native as native
from tools.react_native.bleng_scan_evidence import (
    NOTIFICATION_SCENE,
    SCAN_SCENE,
)

DEVICE = "00008110-000C61160C39401E"
LOCAL_NAME = "BleHubMultiRfV2DKA"
SERVICE_UUID = "fd600000-0000-4000-8000-000000000000"
CHARACTERISTIC = "fd600000-0000-4000-8000-000000000001"
VALUE_HEX = "0102030405"
SUBSCRIPTION = {"uuid": CHARACTERISTIC, "mode": "notification", "value": VALUE_HEX}
IO = {"uuid": CHARACTERISTIC, "write": "7"}
CCC_SUBSCRIPTION = {"uuid": CHARACTERISTIC, "mode": "notification", "value": "ef"}
REFUSAL = 14  # `fault_control.REJECT_ATT_RESULT`, the firmware contract's ATT status.
IO_PROPERTIES = "read,write,writeNoResponse"


def _frame(**values: str) -> dict[str, str]:
    base = {key: "none" for key in run_status.KEYS}
    base.update(values)
    return base


def _scan_projection() -> dict[str, dict[str, str]]:
    return {
        "initial": _frame(state="idle", page="Tests", scan="idle"),
        "started": _frame(state="started", scan="idle"),
        "scanning": _frame(
            state="started", scan="running", scanTotal="2", scanFilter=SERVICE_UUID
        ),
        "stopped": _frame(state="started", scan="stopped"),
        "closed": _frame(state="closed", scan="ownerClosed"),
    }


def _ledger(*actions: str) -> str:
    entries = "\n".join(
        f"{index}|{action}|succeeded|" for index, action in enumerate(actions, start=1)
    )
    return f"omitted=0\n{entries}"


def _plan_steps_rows(steps, *, wait_any: int | None = None) -> list[dict]:
    """One receipt row per plan step, as the driver would publish it.

    A row carries what the step observed: an advertisement for a pull loop, and the branch a
    disjunction resolved to. Building the receipt from the plan keeps the fake honest about
    which step produced which observation.
    """
    rows: list[dict] = []
    for index, step in enumerate(steps):
        observed: dict[str, str] = {}
        if step.get("op") == "tapUntil":
            observed["scan-latest"] = f'exists label=name="{LOCAL_NAME}"; rssi=-50'
        if step.get("op") == "waitAny" and wait_any is not None:
            observed["waitAny"] = str(wait_any)
        if step.get("op") == "still":
            observed["still"] = f"{step.get('for')}s"
        rows.append(
            {"index": index, "step": step, "seconds": 0.1, "observed": observed}
        )
    return rows


def _receipt(
    steps: int,
    ledger: str,
    *,
    values: dict | None = None,
    plan_steps=None,
    wait_any: int | None = None,
) -> dict:
    return {
        "scope": "ios-scene-driver",
        "status": "passed",
        "primary_error": "",
        "steps": (
            _plan_steps_rows(plan_steps, wait_any=wait_any)
            if plan_steps is not None
            else [
                {
                    "index": index,
                    "step": {"op": "tapUntil", "id": "next-scan-event"},
                    "seconds": 0.5,
                    "observed": {
                        "scan-latest": f'exists label=name="{LOCAL_NAME}"; rssi=-50'
                    },
                }
                for index in range(steps)
            ]
        ),
        "values": {"ledger": ledger, **(values or {})},
    }


def _lane_result(
    run_id: str,
    plan: str,
    steps: int,
    projection: dict[str, dict[str, str]],
    ledger: str,
    *,
    status: str = "passed",
    values: dict | None = None,
    plan_steps=None,
    wait_any: int | None = None,
) -> dict:
    # Every iOS phase exports a hierarchy, so every fake result carries one: a test that
    # wanted a phase without a diagnostic would be describing a lane that never ran.
    rows = [
        {
            "name": "ios-scene-driver-app-hierarchy_0_AB.txt",
            "file": "ABC.txt",
            "bytes": 9,
        }
    ]
    return {
        "scope": "ios-scene-driver",
        "run_id": run_id,
        "device": DEVICE,
        "plan": plan,
        "steps": steps,
        "step_seconds_total": 1.5,
        "xctestrun_sha256": "0" * 64,
        "xcodebuild_returncode": 0 if status == "passed" else 65,
        "status": status,
        "primary_error": None if status == "passed" else "scene failed",
        "receipt": _receipt(
            steps,
            ledger,
            values=values,
            plan_steps=plan_steps,
            wait_any=wait_any,
        ),
        "attachments": rows,
        "projection": projection,
    }


class NativeDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.xctestrun = self.root / "host.xctestrun"
        self.xctestrun.write_text("<plist/>", encoding="utf-8")
        self.output = self.root / "attempt" / "driver"
        self.run_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _driver(self, *, scene=SCAN_SCENE, subscription=None, gatt=None, output=None):
        driver = native.NativeIosSceneDriver(
            scene=scene,
            device=DEVICE,
            app_id="FWVYQ287W5.bleng-rn-host",
            xctestrun=self.xctestrun,
            output=self.output if output is None else output,
            cwd=self.root,
            expected_local_name=LOCAL_NAME,
            service_filter=SERVICE_UUID,
            subscription=subscription,
            gatt=gatt,
        )
        # The envelope always calls `require_handoff` first; this is that post-handoff
        # state, and it is what makes `session` decodable.
        driver._run_id = self.run_id
        return driver

    def _options(self, observations: list[str] | None = None, **extra) -> dict:
        options: dict = {
            "timeout": 120.0,
            "observations": observations if observations is not None else [],
            "require_app_authority": extra.pop("require_app_authority", lambda: None),
        }
        options.update(extra)
        return options

    def _fake_lane(self, results: dict[str, dict]):
        def run_scene(*, output_root, run_id, plan_name, **_kwargs):
            result = results[plan_name]
            attempt = Path(output_root) / run_id
            attempt.mkdir(parents=True)
            rows = result["attachments"]
            if rows:
                folder = attempt / "attachments"
                folder.mkdir()
                (folder / "ABC.txt").write_text("hierarchy", encoding="utf-8")
            (attempt / "result.json").write_text(json.dumps(result), encoding="utf-8")
            if result["status"] != "passed":
                raise RuntimeError(f"scene plan failed: {plan_name}")
            return attempt / "result.json"

        return run_scene

    # -- construction ---------------------------------------------------------

    # The scene-side refusals (a missing phase list, an unnamed peer, a missing profile
    # target, an unusable payload) are `scene_native`'s and live in `test_scene_semantics.py`.
    # What is left here is what only this executor can answer: the artifact it pins and the
    # platform it refuses.

    def test_refuses_a_missing_xctestrun(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing xctestrun"):
            native.NativeIosSceneDriver(
                scene=SCAN_SCENE,
                device=DEVICE,
                app_id="FWVYQ287W5.bleng-rn-host",
                xctestrun=self.root / "absent.xctestrun",
                output=self.output,
                cwd=self.root,
                expected_local_name=LOCAL_NAME,
            )

    def test_the_driver_is_ios_only(self) -> None:
        driver = self._driver()
        with self.assertRaisesRegex(TypeError, "iOS-only"):
            driver.require_handoff(
                platform="android",
                candidate=None,
                phone=None,
                run_id=self.run_id,
                app_id="FWVYQ287W5.bleng-rn-host",
                deadline=10_000_000_000.0,
            )

    def test_it_offers_no_per_click_identity_tier(self) -> None:
        # An in-process target owns the taps it makes; the checkpoint brackets the scene.
        self.assertIsNone(self._driver().click_identity())

    # -- single-phase scene ---------------------------------------------------

    def test_a_passed_scan_publishes_derived_witnesses_only(self) -> None:
        driver = self._driver()
        observations: list[str] = []
        steps = len(driver._steps(driver._phases[0]))
        result = _lane_result(
            self.run_id,
            "scan",
            steps,
            _scan_projection(),
            _ledger(
                "start",
                "begin-scan",
                "scan-start-wait",
                "scan-pull",
                "scan-stop",
                "close",
            ),
        )
        with patch.object(
            native.lane, "run_scene", side_effect=self._fake_lane({"scan": result})
        ):
            driver.drive(SCAN_SCENE, self._options(observations))
        self.assertEqual(observations, list(SCAN_SCENE.trace))
        published = {path.name for path in self.output.iterdir()}
        self.assertEqual(
            published,
            {
                "plan.json",
                "record.json",
                "phase-scan.json",
                "receipt-scan.json",
                "diagnostic-scan-0-ios-scene-driver-app-hierarchy-0-ab-txt.txt",
            },
        )
        record = json.loads((self.output / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["kind"], native.KIND)
        self.assertEqual(record["steps"], steps)
        (phase,) = record["phases"]
        self.assertEqual(phase["name"], "scan")
        self.assertEqual(phase["status"], "passed")
        self.assertEqual(phase["xcodebuild_returncode"], 0)
        self.assertEqual(phase["result"], "phase-scan.json")

    def test_a_repeating_phase_name_is_a_safe_file_name(self) -> None:
        # A repeating scene's phases are `scan:0` / `announce:0`. A colon is not a safe path
        # segment, and the bundle publisher refuses it *after* the file has been written, so a
        # passing scene failed at publication. Slugging the name is what keeps every phase
        # addressable on both platforms (`scene_android_native` already does this).
        driver = self._driver()
        self.output.mkdir(parents=True)
        raw = self.root / "raw"
        source = raw / "scan:0" / self.run_id / "attachments" / "ABC.txt"
        source.parent.mkdir(parents=True)
        source.write_text("hierarchy", encoding="utf-8")
        driver._publish_diagnostics(
            raw,
            native.Phase("scan:0"),
            {
                "attachments": [
                    {
                        "name": "ios-scene-driver-app-hierarchy_0_AB.txt",
                        "file": "ABC.txt",
                        "bytes": 9,
                    }
                ]
            },
        )
        self.assertEqual(
            sorted(path.name for path in self.output.iterdir()),
            ["diagnostic-scan-0-0-ios-scene-driver-app-hierarchy-0-ab-txt.txt"],
        )

    def test_a_failed_scene_still_publishes_and_then_raises(self) -> None:
        driver = self._driver()
        steps = len(driver._steps(driver._phases[0]))
        result = _lane_result(
            self.run_id,
            "scan",
            steps,
            _scan_projection(),
            _ledger(
                "start",
                "begin-scan",
                "scan-start-wait",
                "scan-pull",
                "scan-stop",
                "close",
            ),
            status="failed",
        )
        observations: list[str] = []
        with (
            patch.object(
                native.lane, "run_scene", side_effect=self._fake_lane({"scan": result})
            ),
            self.assertRaisesRegex(RuntimeError, "scene plan failed"),
        ):
            driver.drive(SCAN_SCENE, self._options(observations))
        self.assertEqual(observations, [])
        record = json.loads((self.output / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertIn("scene plan failed", record["primary_error"])
        self.assertEqual(record["phases"][0]["status"], "failed")
        self.assertEqual(record["phases"][0]["xcodebuild_returncode"], 65)
        self.assertTrue((self.output / "receipt-scan.json").is_file())
        # A failed phase keeps the screen record its export produced; the receipt is what
        # says the scene failed, the hierarchy is what says the screen looked like.
        self.assertTrue(
            (
                self.output
                / "diagnostic-scan-0-ios-scene-driver-app-hierarchy-0-ab-txt.txt"
            ).is_file()
        )

    def test_a_scene_that_never_runs_publishes_a_failed_record(self) -> None:
        driver = self._driver()

        def explode(**_kwargs):
            raise RuntimeError("xcodebuild failed to start")

        with (
            patch.object(native.lane, "run_scene", side_effect=explode),
            self.assertRaisesRegex(RuntimeError, "failed to start"),
        ):
            driver.drive(SCAN_SCENE, self._options())
        record = json.loads((self.output / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["phases"][0]["xcodebuild_returncode"])
        self.assertIsNone(record["phases"][0]["result"])

    # -- two-phase scene ------------------------------------------------------

    def test_each_phase_is_one_attach_run_against_the_same_app(self) -> None:
        # The iOS executor's own fact, and the only one this module still states about a
        # multi-phase scene: each phase is a separate `test-without-building` run against the
        # App the host installed, never a relaunch. What the phases then *mean* is derived by
        # `scene_native` and covered executor-free in `test_scene_semantics.py`.
        driver = self._driver(scene=NOTIFICATION_SCENE, subscription=SUBSCRIPTION)
        driver._run_id = self.run_id
        runs: list[tuple[str, bool]] = []

        def run_scene(**kwargs) -> Path:
            runs.append((kwargs["plan_name"], kwargs["launch"]))
            result = self.root / f"{kwargs['plan_name']}.json"
            result.write_text(json.dumps({"xcodebuild_returncode": 0}))
            return result

        with patch.object(native.lane, "run_scene", run_scene):
            for phase in driver._phases:
                driver._run_phase(phase, self.root, self._options(), timeout=30.0)

        self.assertEqual(
            runs,
            [("notification.subscribe", False), ("notification.collect", False)],
        )


if __name__ == "__main__":
    unittest.main()
