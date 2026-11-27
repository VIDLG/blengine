"""Producer-level double for the Android in-process executor.

Producer tests assert the envelope's own contracts: lease ordering, fixture retirement,
candidate quarantine and evidence completeness. Those do not depend on what the executor
does on the device, so this double replaces exactly that one seam — one scene-wide run —
and leaves the shared driver (merged plan, handoff ordering, phase slicing, record
publication), the prepared envelope, the producer and the bundle verifier running for real.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.react_native.scene_android_native import NativeAndroidSceneDriver


class FakeNativeAndroidSceneDriver(NativeAndroidSceneDriver):
    """The real driver with a synthetic executor."""

    def _run_scene_plan(
        self,
        raw: Path,
        options: dict[str, Any],
        *,
        timeout: float,
        on_handoff: Callable[[str], None],
    ) -> dict[str, Any]:
        steps = self._merged_steps()
        # A real scene performs each host action at its handoff; the double keeps the
        # ordering visible to the envelope, which is what these tests assert.
        for phase in self._phases:
            if phase.host is not None:
                on_handoff(phase.host)
        rows = [
            {"index": index, "step": step, "seconds": 0.0, "observed": {}}
            for index, step in enumerate(steps)
        ]
        return {
            "schema_version": 1,
            "scope": "formal-android-scene-driver",
            "run_id": self._run_id,
            "platform": "android",
            "device": self._device,
            "app_id": self._app_id,
            "plan": self._scene.name,
            "steps": len(steps),
            "timeout": timeout,
            "test_apk_sha256": self._artifact_sha256,
            "receipt": {
                "scope": "android-scene-driver",
                "status": "passed",
                "primary_error": "",
                "steps": rows,
                "values": {},
            },
            "hierarchy": None,
            "projection": {},
            "step_seconds_total": 0.0,
            "instrumentation_returncode": 0,
            "instrumentation_seconds": 0.0,
            "instrumentation_lingered": False,
            "handoffs": [
                phase.host for phase in self._phases if phase.host is not None
            ],
            "status": "passed",
            "primary_error": None,
            "primary_traceback": None,
        }

    def _observations(self, per_phase: dict[str, dict[str, Any]]) -> list[str]:
        return list(self._scene.trace)
