"""Drive one managed Android scene with the App's own instrumentation, in-process.

The scene vocabulary — phases, host actions, ledgers and the twelve observation derivations —
lives in `scene_native`. This module is only the Android *executor*: it pins the test APK, binds
the App the host installed and launched, runs one `am instrument` per phase, and names the
driver record's kind and scope.

The plan the driver runs is the same file the iOS target reads, and the identifiers it acts on
are the same `testID`s React Native exposes as accessibility resource IDs. What differs is the
executor (UiAutomator instead of `XCUIElement`), and that difference is stated by the record's
kind rather than hidden in the evidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.android.state_control import AndroidRadioController
from tools.file_digest import sha256_file

from . import run_android_scene_driver as lane
from .bleng_android_candidate import AndroidCandidateOwner
from .bleng_scan_evidence import ANDROID_NATIVE_DRIVER_KIND, SceneEvidence
from .scene_native import (
    GATT_TARGET_KEYS,
    IO_LEDGER,
    PHASES,
    PLANNED_SCENES,
    REPEATING_SCENES,
    NativeSceneDriver,
    Phase,
    _slug,
    phases_for,
)

RECORD_SCOPE = "formal-android-native-scene-driver"
_ACTIVITY = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_$]*)+")


class NativeAndroidSceneDriver(NativeSceneDriver):
    """One `am instrument` run per scene; nothing resident, nothing to retire."""

    kind = ANDROID_NATIVE_DRIVER_KIND
    record_scope = RECORD_SCOPE
    # An instrumented test runs inside the target package's own process and replaces any
    # process already there, so one scene is one process with in-band host actions.
    one_process = True

    def __init__(
        self,
        *,
        scene: SceneEvidence,
        device: str,
        app_id: str,
        activity: str,
        test_apk: Path,
        output: Path,
        cwd: Path,
        fresh_install: bool = True,
        expected_local_name: str | None = None,
        service_filter: str | None = None,
        subscription: dict[str, str] | None = None,
        gatt: dict[str, str] | None = None,
    ) -> None:
        if type(activity) is not str or _ACTIVITY.fullmatch(activity) is None:
            raise ValueError("an explicit fully qualified Android activity is required")
        if not test_apk.is_file() or test_apk.is_symlink():
            raise ValueError(f"missing test APK: {test_apk}")
        self._activity = activity
        self._test_apk = test_apk.absolute()
        self._artifact_sha256 = sha256_file(self._test_apk)
        super().__init__(
            scene=scene,
            device=device,
            app_id=app_id,
            output=output,
            cwd=cwd,
            fresh_install=fresh_install,
            expected_local_name=expected_local_name,
            service_filter=service_filter,
            subscription=subscription,
            gatt=gatt,
        )

    # -- platform seams -------------------------------------------------------

    def _artifact(self) -> dict[str, Any]:
        return {
            "test_apk_sha256": self._artifact_sha256,
            "test_component": lane.TEST_COMPONENT,
            "test_class": lane.TEST_CLASS,
        }

    def _require_artifact(self) -> None:
        if sha256_file(self._test_apk) != self._artifact_sha256:
            raise RuntimeError("the scene driver artifact changed during the run")

    def _require_platform(self, *, platform: str, candidate: Any, phone: Any) -> None:
        if platform != "android":
            raise TypeError("the in-process instrumentation driver is Android-only")
        if type(candidate) is not AndroidCandidateOwner:
            raise TypeError("the native driver requires an Android candidate owner")
        if type(phone) is not AndroidRadioController:
            raise TypeError("the native driver requires an Android phone lease")

    def _bind_candidate(
        self, candidate: Any, *, require_owner: Callable[[], None], deadline: float
    ) -> Callable[[], None]:
        # Instrumentation runs as the App's own process, so there is no remote session to
        # bind. The checkpoint is still the App's OS identity, read fresh against the launch
        # this run performed; it brackets the scene rather than every click.
        return candidate.bind_native(require_owner=require_owner, deadline=deadline)

    def _run_phase(
        self, phase: Phase, raw: Path, options: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """Single-phase scenes are still one run of the merged plan."""
        return self._run_scene_plan(
            raw, options, timeout=timeout, on_handoff=lambda action: None
        )

    def _run_scene_plan(
        self,
        raw: Path,
        options: dict[str, Any],
        *,
        timeout: float,
        on_handoff: Callable[[str], None],
    ) -> dict[str, Any]:
        steps = self._merged_steps()
        name = self._scene.name
        result_path = lane.run_scene(
            device=self._device,
            plan_name=name,
            output_root=raw / name,
            timeout=timeout,
            # The host installed and launched this App; the plan foregrounds it.
            launch=False,
            app_id=self._app_id,
            activity=self._activity,
            test_apk_sha256=self._artifact_sha256,
            fresh_install=self._fresh_install,
            expected_local_name=self._expected_local_name,
            service_filter=self._service_filter,
            steps=steps,
            cwd=self._cwd,
            run_id=f"{self._run_id}",
            on_handoff=on_handoff,
            # The driver polls its own process every 200 ms; a per-poll owner callback would
            # turn one scene into thousands of device reads. Ownership is re-proved at the
            # scene boundaries (`bind`) and by the envelope around every host action.
            require_owner=None,
        )
        return json.loads(result_path.read_text(encoding="utf-8"))

    def _phase_extra(self, result: dict[str, Any]) -> dict[str, Any]:
        return {"instrumentation_returncode": result.get("instrumentation_returncode")}

    def _publish_diagnostics(
        self, raw: Path, phase: Phase, result: dict[str, Any]
    ) -> None:
        """Copy the phase's bounded hierarchy dump beside the receipts."""
        source = raw / self._scene.name / str(self._run_id) / "hierarchy.xml"
        if not source.is_file():
            return
        name = f"diagnostic-{_slug(phase.name)}-hierarchy.xml"
        with (self._output / name).open("xb") as stream:
            stream.write(source.read_bytes()[: 64 * 1024])


__all__ = [
    "GATT_TARGET_KEYS",
    "IO_LEDGER",
    "PHASES",
    "PLANNED_SCENES",
    "REPEATING_SCENES",
    "NativeAndroidSceneDriver",
    "Phase",
    "phases_for",
]
