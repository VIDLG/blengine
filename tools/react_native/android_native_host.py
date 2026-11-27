"""Android preparation for the in-process scene lane: install, launch, nothing resident.

The scene runs inside the App's own process, so no automation server, no helper APKs and no
temporary settings exist for this lane to prepare. It installs the App and the instrumentation
APK, launches the App, and lets the driver's own test process drive it. The surface is the
producer's (`prepare`, `launch`, `candidate`, `close_automation`, `require_automation_closed`),
with the server-shaped parts honestly empty rather than faked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TextIO

from tools.android.state_control import AndroidRadioController
from tools.file_digest import sha256_file

from . import android_sdk
from .bleng_android_candidate import AndroidCandidateOwner

_SERIAL = re.compile(r"[A-Za-z0-9_.:-]+")
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
_ACTIVITY = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_$]*)+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_android_inputs(
    *,
    device: str,
    app_id: str,
    activity: str,
    apk: Path,
    apk_sha256: str,
    output_root: Path,
    cwd: Path,
) -> None:
    """Refuse a run whose identities are not explicit, canonical and local.

    Nothing here touches a device: these are the inputs every later stage is allowed to
    assume, so they are checked once, before a lease or an install exists.
    """
    if type(device) is not str or _SERIAL.fullmatch(device) is None:
        raise ValueError("an explicit canonical Android serial is required")
    if type(app_id) is not str or _PACKAGE.fullmatch(app_id) is None:
        raise ValueError("an explicit canonical Android package is required")
    if type(activity) is not str or _ACTIVITY.fullmatch(activity) is None:
        raise ValueError("an explicit fully qualified Android activity is required")
    if not isinstance(apk, Path) or not apk.is_absolute():
        raise ValueError("an absolute APK path is required")
    if apk.is_symlink() or not apk.is_file():
        raise ValueError(f"missing APK: {apk}")
    if apk.suffix != ".apk":
        raise ValueError("the candidate artifact must be an .apk")
    if type(apk_sha256) is not str or _SHA256.fullmatch(apk_sha256) is None:
        raise ValueError("reviewed lowercase APK SHA256 required")
    for name, path in (("output_root", output_root), ("cwd", cwd)):
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise ValueError(f"{name} must be an absolute non-symlink path")


class AndroidNativeHost:
    """Install the App and its instrumentation APK; bind neither to a server."""

    def __init__(
        self,
        *,
        phone: AndroidRadioController,
        run_id: str,
        app_id: str,
        activity: str,
        apk: Path,
        apk_sha256: str,
        test_apk: Path,
        test_apk_sha256: str,
        attempt: Path,
        cwd: Path,
        deadline: float,
        use_root: bool,
    ) -> None:
        if not test_apk.is_file() or test_apk.is_symlink():
            raise ValueError(f"missing test APK: {test_apk}")
        if sha256_file(test_apk) != test_apk_sha256:
            raise ValueError("test APK does not match the reviewed SHA-256")
        self._phone, self._run_id, self._app_id = phone, run_id, app_id
        self._activity, self._apk, self._apk_sha256 = activity, apk, apk_sha256
        self._attempt, self._cwd, self._deadline = attempt, cwd, deadline
        self._use_root = use_root
        # The instrumentation artifact belongs to the platform evidence directory: it is
        # part of what was prepared for this run, not a root-level bundle artifact.
        self._platform = attempt / "platform"
        self._platform.mkdir(mode=0o700, exist_ok=True)
        # A frozen copy is what gets installed, so the bytes cannot change under the run.
        self._test_apk = self._platform / "test.apk"
        with test_apk.open("rb") as source, self._test_apk.open("xb") as frozen:
            frozen.write(source.read())
        if sha256_file(self._test_apk) != test_apk_sha256:
            raise ValueError("frozen test APK does not match the reviewed SHA-256")
        self.candidate: AndroidCandidateOwner | None = None
        self._prepared = self._launched = self._closed = False

    @property
    def test_apk(self) -> Path:
        """The frozen copy that was installed, so the driver pins the same bytes."""
        return self._test_apk

    def prepare(self) -> None:
        """Install the instrumentation APK under the phone lease."""
        if self._closed or self._prepared:
            raise RuntimeError("Android native prepare is one-shot")
        self._prepared = True
        log_path = self._platform / "install-test.log"
        with log_path.open("x", encoding="utf-8") as log:
            self._install(log)

    def _install(self, log: TextIO) -> None:
        android_sdk.install_apk(
            self._phone.device,
            self._test_apk,
            log=log,
            test_only=True,
            use_root=self._use_root,
            deadline=self._deadline,
            require_owner=self._phone.require_owner,
            cwd=self._cwd,
        )

    def launch(self) -> None:
        """Install and launch the candidate App; binding is the driver's job."""
        if self._closed or self._launched or not self._prepared:
            raise RuntimeError(
                "Android native launch requires preparation and is one-shot"
            )
        self._launched = True
        self.candidate = AndroidCandidateOwner(
            apk=self._apk,
            expected_sha256=self._apk_sha256,
            package=self._app_id,
            activity=self._activity,
            phone=self._phone,
            serial=self._phone.device,
            run_id=self._run_id,
            attempt=self._attempt / "candidate",
            cwd=self._cwd,
            deadline=self._deadline,
            test_only=False,
            use_root=self._use_root,
            # The scene driver runs inside this package's own process.
            driver_runs_in_app_process=True,
        )
        self.candidate.install_and_launch()

    def close_automation(self, *, deadline: float) -> None:
        """Nothing resident: the instrumentation process ends with its own `am instrument`."""
        if self._closed:
            return
        self._closed = True

    def require_automation_closed(self) -> None:
        """No server and no helper settings exist in this lane, so there is nothing to prove."""
        if not self._closed:
            raise RuntimeError("Android native lane has no verified retirement")
