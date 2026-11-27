"""Drive one managed iOS scene with the App's own XCTest target, in-process.

The scene vocabulary — phases, host actions, ledgers and the twelve observation derivations —
lives in `scene_native`, because none of it depends on the platform. This module is only the
iOS *executor*: it pins the xctestrun, binds the App the host installed and launched, runs one
`test-without-building` per phase, and names the driver record's kind and scope.

Comparing an iOS run with an Android one is therefore a comparison of executors, not of scene
semantics: both run the same `scene_plans` steps over the same identifiers and publish the same
receipt shape, which is what makes the two lanes' evidence comparable at all.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.file_digest import sha256_file

from . import run_ios_scene_driver as lane
from .bleng_ios_candidate import IosCandidateOwner
from .bleng_scan_evidence import IOS_NATIVE_DRIVER_KIND, SceneEvidence
from .ios_radio_lease import IosRadioLease
from .scene_native import (
    CANCELLATION_LEDGER_PREFIX,
    CANCELLATION_LEDGER_TAILS,
    CANCELLATION_READ_ACTIONS,
    GATT_TARGET_KEYS,
    IO_LEDGER,
    PHASES,
    PLANNED_SCENES,
    REPEATING_SCENES,
    SCAN_LEDGER,
    SUBSCRIPTION_KINDS,
    SUBSCRIPTION_LEDGER,
    WRITE_REJECT_LEDGER,
    NativeSceneDriver,
    Phase,
    phases_for,
)
from .scene_plans import CONTROLS_REQUIRING_LINK

KIND = IOS_NATIVE_DRIVER_KIND
RECORD_SCOPE = "formal-ios-native-scene-driver"


class NativeIosSceneDriver(NativeSceneDriver):
    """One in-process XCTest phase at a time; nothing resident, nothing to retire."""

    kind = KIND
    record_scope = RECORD_SCOPE

    def __init__(
        self,
        *,
        scene: SceneEvidence,
        device: str,
        app_id: str,
        xctestrun: Path,
        output: Path,
        cwd: Path,
        fresh_install: bool = True,
        expected_local_name: str | None = None,
        service_filter: str | None = None,
        subscription: dict[str, str] | None = None,
        gatt: dict[str, str] | None = None,
    ) -> None:
        if not xctestrun.is_file() or xctestrun.is_symlink():
            raise ValueError(f"missing xctestrun: {xctestrun}")
        self._xctestrun = xctestrun.absolute()
        self._artifact_sha256 = sha256_file(self._xctestrun)
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
        return {"xctestrun_sha256": self._artifact_sha256}

    def _require_artifact(self) -> None:
        if sha256_file(self._xctestrun) != self._artifact_sha256:
            raise RuntimeError("the scene driver artifact changed during the run")

    def _require_platform(self, *, platform: str, candidate: Any, phone: Any) -> None:
        if platform != "ios":
            raise TypeError("the in-process XCTest driver is iOS-only")
        if type(candidate) is not IosCandidateOwner:
            raise TypeError("the native driver requires an iOS candidate owner")
        if type(phone) is not IosRadioLease:
            raise TypeError("the native driver requires an iOS phone lease")

    def _bind_candidate(
        self, candidate: Any, *, require_owner: Callable[[], None], deadline: float
    ) -> Callable[[], None]:
        return candidate.bind_native(require_owner=require_owner, deadline=deadline)

    def _run_phase(
        self, phase: Phase, raw: Path, options: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        result_path = lane.run_scene(
            device=self._device,
            xctestrun=self._xctestrun,
            plan_name=self._plan_name(phase),
            output_root=raw / phase.name,
            timeout=timeout,
            # The host installed, launched and pinned this App; the plan foregrounds it.
            launch=False,
            fresh_install=self._fresh_install,
            expected_local_name=self._expected_local_name,
            service_filter=self._service_filter,
            steps=self._steps(phase),
            cwd=self._cwd,
            run_id=f"{self._run_id}",
        )
        return json.loads(result_path.read_text(encoding="utf-8"))

    def _phase_extra(self, result: dict[str, Any]) -> dict[str, Any]:
        return {"xcodebuild_returncode": result.get("xcodebuild_returncode")}

    def _publish_diagnostics(
        self, raw: Path, phase: Phase, result: dict[str, Any]
    ) -> None:
        """Copy the exported hierarchy dumps beside the receipts, bounded."""
        for index, row in enumerate(result.get("attachments") or []):
            source = (
                raw
                / phase.name
                / str(self._run_id)
                / "attachments"
                / str(row.get("file"))
            )
            if not source.is_file() or source.suffix != ".txt":
                continue
            name = "diagnostic-{}-{}-{}.txt".format(
                _slug(phase.name), index, _slug(str(row.get("name")))
            )
            with (self._output / name).open("xb") as stream:
                stream.write(source.read_bytes()[: 64 * 1024])


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:48] or "attachment"


__all__ = [
    "CANCELLATION_LEDGER_PREFIX",
    "CANCELLATION_LEDGER_TAILS",
    "CANCELLATION_READ_ACTIONS",
    "CONTROLS_REQUIRING_LINK",
    "GATT_TARGET_KEYS",
    "IO_LEDGER",
    "KIND",
    "PHASES",
    "PLANNED_SCENES",
    "REPEATING_SCENES",
    "SCAN_LEDGER",
    "SUBSCRIPTION_KINDS",
    "SUBSCRIPTION_LEDGER",
    "WRITE_REJECT_LEDGER",
    "NativeIosSceneDriver",
    "Phase",
    "phases_for",
]
