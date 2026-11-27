"""One-shot formal iOS install/launch provenance, not a UI/RF runner.

The caller supplies a frozen device App, an active observation lease and one
absolute deadline. This owner never selects a phone, signs/builds an App, changes
radio/permissions, acquires a lease, or repairs an uncertain launch. Raw receipts
prove the commands submitted in this run, NOT an installed binary content hash.
"""

from __future__ import annotations

import json
import math
import plistlib
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tools.apple.app_artifact import fingerprint

from . import ios_sdk

# The floor `devicectl` enforces on its own `--timeout`; a smaller value is a usage error, not
# a short wait, so every budget handed to it is checked against this.
DEVICECTL_MIN_TIMEOUT = 5
from .capture_io import publish_new_json
from .ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease


class _ScenarioDeadlineExceeded(TimeoutError):
    """An exhausted scenario budget does not revoke separately checked cleanup."""


class IosCandidateOwner:
    """Own one managed installation and launch; no implicit cleanup on failure.

    ``install_and_launch`` is one-shot even on failure. ``bind`` then supplies the
    concrete provenance callbacks to the existing fresh native identity protocol.
    A failed bind cannot be repinned. The host must preserve primary/cleanup errors
    and quarantine uncertain ownership; ``retire`` only revokes this local authority,
    never releases the phone or claims that native resources have been cleaned up.
    """

    def __init__(
        self,
        *,
        app: Path,
        expected_sha256: str,
        bundle_id: str,
        executable: str,
        phone: IosRadioLease,
        device: str,
        run_id: str,
        attempt: Path,
        cwd: Path,
        deadline: float,
        release_running: bool = False,
    ) -> None:
        if (
            type(device) is not str
            or re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}", device) is None
            or not ios_sdk._identity_uuid(run_id)
            or type(expected_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or type(bundle_id) is not str
            or re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id) is None
            or type(executable) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]+", executable) is None
        ):
            raise ValueError(
                "explicit physical device/run/candidate/App identity required"
            )
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite absolute monotonic deadline required")
        self._app = app.absolute()
        self._sha256 = expected_sha256
        self._bundle_id = bundle_id
        self._executable = executable
        self._phone = phone
        self._device = device
        if type(release_running) is not bool:
            raise ValueError("release_running requires an explicit boolean")
        self._release_running = release_running
        self._run_id = run_id
        self._attempt = attempt.absolute()
        self._cwd = cwd.absolute()
        self._deadline = deadline
        self._started = False
        self._ready = False
        self._retired = False
        self._binding_attempted = False
        self._identity: ios_sdk.IosAppIdentity | None = None
        self._finalization_started = False
        self._failure: Exception | None = None
        self._core_device: str | None = None
        self._receipts: list[tuple[Path, tuple[str, ...], str]] = []
        self._guard()
        if self._app.suffix != ".app":
            raise ValueError("a frozen device .app is required")
        self.read_candidate_sha256()
        info = plistlib.loads((self._app / "Info.plist").read_bytes())
        if (
            type(info) is not dict
            or info.get("CFBundleIdentifier") != bundle_id
            or info.get("CFBundleExecutable") != executable
            or info.get("CFBundlePackageType") != "APPL"
            or info.get("CFBundleSupportedPlatforms") != ["iPhoneOS"]
            or not (self._app / executable).is_file()
            or (self._app / executable).stat().st_size == 0
        ):
            raise ValueError(
                "frozen App must match the explicit device bundle/executable"
            )
        self.read_candidate_sha256()
        # Never overwrite historical receipts or share an attempt directory.
        self._attempt.mkdir(mode=0o700)

    @property
    def install_receipt(self) -> Path:
        return self._attempt / "02-install.json"

    @property
    def launch_receipt(self) -> Path:
        return self._attempt / "04-launch.json"

    def _budget(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _ScenarioDeadlineExceeded("iOS candidate total deadline exceeded")
        return remaining

    @contextmanager
    def _checking(self) -> Iterator[None]:
        if self._failure is not None:
            raise RuntimeError("iOS candidate authority invalidated") from self._failure
        try:
            yield
        except _ScenarioDeadlineExceeded:
            raise
        except Exception as error:
            if self._failure is None:
                self._failure = error
            raise

    def _guard(self) -> None:
        with self._checking():
            self._require_current()

    def _require_current(self) -> None:
        self._budget()
        if self._retired:
            raise RuntimeError("iOS candidate owner retired")
        self._require_phone_owner()
        self._budget()

    def _require_phone_owner(self) -> None:
        phone = self._phone
        if (
            type(phone) is not IosRadioLease
            or phone.device != self._device
            or phone.run_id != self._run_id
            or phone.scope != SCAN_OBSERVATION_SCOPE
            or phone.document.get("device") != self._device
            or phone.document.get("run_id") != self._run_id
            or phone.document.get("scope") != SCAN_OBSERVATION_SCOPE
        ):
            raise RuntimeError("iOS candidate phone/run ownership changed")
        phone.require_owner()

    def read_candidate_sha256(self) -> str:
        with self._checking():
            self._guard()
            digest = fingerprint(self._app, require_current=self._guard)
            self._guard()
            if digest != self._sha256:
                raise RuntimeError("frozen iOS candidate changed")
            return digest

    def _check_receipts(self, require_current: Callable[[], None]) -> None:
        for path, arguments, digest in self._receipts:
            require_current()
            result, current = ios_sdk._identity_official(
                path, ".".join(arguments[:4]), self._device, list(arguments)
            )
            if current != digest or result["deviceIdentifier"] != self._core_device:
                raise RuntimeError("managed iOS receipt changed")
        require_current()

    def require_candidate_provenance(self) -> None:
        """Read-only local provenance, not a cached live-process/RF verdict.

        The App binding separately calls ``read_candidate_sha256`` at each boundary;
        do not hash the whole App a second time here. This guard freezes the managed
        receipts and lease. Native boot/process checks belong to ``bind_native``.
        """
        self._guard()
        if not self._ready:
            raise RuntimeError("managed iOS candidate not ready")
        with self._checking():
            self._check_receipts(self._guard)

    def _sdk_timeout(self) -> int:
        seconds = int(min(30.0, self._budget()))
        if seconds < DEVICECTL_MIN_TIMEOUT:
            raise TimeoutError(
                "insufficient iOS candidate SDK deadline budget: devicectl needs at least "
                f"{DEVICECTL_MIN_TIMEOUT}s"
            )
        return seconds

    def _sdk(self, command: list[str]) -> dict[str, Any]:
        self._guard()
        self.read_candidate_sha256()
        self._check_receipts(self._guard)
        receipt = Path(command[command.index("--json-output") + 1])
        if receipt.exists() or receipt.is_symlink():
            raise RuntimeError("refusing an existing SDK receipt")
        command[command.index("--timeout") + 1] = str(self._sdk_timeout())
        try:
            ios_sdk.run_logged(
                command,
                cwd=self._cwd,
                log=receipt.with_suffix(".log"),
                timeout=min(30.0, self._budget()),
            )
        except Exception as error:
            try:
                self._guard()
            except Exception as boundary:  # noqa: BLE001 -- keep the primary command failure.
                error.add_note(
                    f"iOS post-command ownership/deadline failure: {boundary}"
                )
            raise
        self._guard()
        self.read_candidate_sha256()
        self._check_receipts(self._guard)
        result, digest = ios_sdk._identity_official(
            receipt, ".".join(command[1:5]), self._device, command[1:]
        )
        core = result["deviceIdentifier"]
        if not ios_sdk._identity_uuid(core.lower()):
            raise RuntimeError("invalid CoreDevice identity")
        if self._core_device is not None and core != self._core_device:
            raise RuntimeError("iOS managed command changed CoreDevice identity")
        self._core_device = core
        self._receipts.append((receipt, tuple(command[1:]), digest))
        self._guard()
        return result

    def _running_pids(self, receipt: Path) -> list[int]:
        """Processes this bundle owns, matched from the installed inventory plus PATH.

        Process inventory need not include bundle IDs, so the installed directory of our
        own bundle id and the executable name are both accepted evidence; anything else is
        left alone.
        """
        installed = self._sdk(
            ios_sdk.devicectl_device_apps(
                self._device,
                receipt.with_stem(receipt.stem + "-apps"),
                timeout=self._sdk_timeout(),
            )
        ).get("apps")
        if type(installed) is not list or any(
            type(app) is not dict or type(app.get("bundleIdentifier")) is not str
            for app in installed
        ):
            raise RuntimeError("malformed installed iOS App inventory")
        matching = [
            app for app in installed if app["bundleIdentifier"] == self._bundle_id
        ]
        if len(matching) > 1:
            raise RuntimeError("ambiguous installed iOS App identity")
        old_directory = None
        if matching:
            url = matching[0].get("url")
            if type(url) is not str or not url.endswith(".app/"):
                raise RuntimeError("malformed installed iOS App directory")
            old_directory = ios_sdk._identity_executable_path(url[:-1], file_url=True)
        rows = self._sdk(
            ios_sdk.devicectl_processes(
                self._device, receipt, timeout=self._sdk_timeout()
            )
        ).get("runningProcesses")
        if type(rows) is not list:
            raise RuntimeError("malformed iOS process inventory")
        seen: set[int] = set()
        running: list[int] = []
        for row in rows:
            if (
                type(row) is not dict
                or type(row.get("processIdentifier")) is not int
                or row["processIdentifier"] <= 0
                or row["processIdentifier"] in seen
            ):
                raise RuntimeError("malformed or ambiguous iOS process inventory")
            seen.add(row["processIdentifier"])
            path = ios_sdk._identity_executable_path(
                row.get("executable"), file_url=True
            )
            if (
                row.get("bundleIdentifier") == self._bundle_id
                or Path(path).name == self._executable
                or (old_directory is not None and path.startswith(old_directory + "/"))
            ):
                running.append(row["processIdentifier"])
        return running

    def _refuse_running(self, receipt: Path) -> None:
        """Refuse a running copy of this App, or release exactly that copy when asked.

        A copy left running by an earlier attempt (or by a human) would otherwise block
        every later run, and the operator cannot be expected to hunt it down: the release
        entry needs its own device lease, which the run already holds. With
        `release_running` the run terminates only the PIDs this inventory attributed to
        our own bundle/installation, records that act, and proves it took effect before
        installing; without it the refusal stands unchanged.
        """
        running = self._running_pids(receipt)
        if not running:
            return
        if not self._release_running:
            raise RuntimeError("formal iOS App already running; refusing takeover")
        record: dict[str, Any] = {
            "schema_version": 1,
            "scope": "formal-ios-pre-install-release",
            "device": self._device,
            "bundle_id": self._bundle_id,
            "requested_pids": running,
            "terminated_pids": [],
            "status": "failed",
            "detail": (
                "One bounded termination of this bundle's own running copy before the "
                "install that replaces it. Not resource-zero, radio state or a gate."
            ),
        }
        for pid in running:
            self._sdk(
                ios_sdk.devicectl_terminate_process(
                    self._device,
                    pid,
                    self._attempt / f"01-before-install-terminate-{pid}.json",
                    timeout=self._sdk_timeout(),
                )
            )
            record["terminated_pids"].append(pid)
        self._guard()
        record["status"] = "passed"
        publish_new_json(self._attempt / "01-before-install-release.json", record)
        # Prove the release took effect instead of trusting the terminate receipt.
        if self._running_pids(receipt.with_stem(receipt.stem + "-after-release")):
            raise RuntimeError(
                "the App this run released is still running; refusing takeover"
            )

    def install_and_launch(self) -> None:
        """Submit once; an exception may mean the remote outcome is unknown."""
        if self._started:
            raise RuntimeError("managed iOS install/launch is one-shot")
        self._started = True
        self._guard()
        self._refuse_running(self._attempt / "01-before-install.json")
        installed = self._sdk(
            ios_sdk.devicectl_install_app(
                self._device,
                self._app,
                self.install_receipt,
                timeout=self._sdk_timeout(),
            )
        )
        rows = installed.get("installedApplications")
        if (
            type(rows) is not list
            or len(rows) != 1
            or type(rows[0]) is not dict
            or rows[0].get("bundleID") != self._bundle_id
            or type(rows[0].get("installationURL")) is not str
            or not rows[0]["installationURL"].endswith(".app/")
        ):
            raise RuntimeError("foreign/malformed managed iOS installation")
        installed_url = rows[0]["installationURL"]
        ios_sdk._identity_executable_path(installed_url[:-1], file_url=True)
        self._refuse_running(self._attempt / "03-before-launch.json")
        arguments = ["--blehub-formal-identity-run-id", self._run_id]
        launched = self._sdk(
            ios_sdk.devicectl_launch_once(
                self._device,
                self._bundle_id,
                self.launch_receipt,
                arguments,
                timeout=self._sdk_timeout(),
            )
        )
        process, options = launched.get("process"), launched.get("launchOptions")
        if (
            type(process) is not dict
            or type(process.get("processIdentifier")) is not int
            or process["processIdentifier"] <= 0
            or process.get("executable") != installed_url + self._executable
            or type(options) is not dict
            or options.get("arguments") != arguments
            or options.get("activatedWhenStarted") is not True
            or options.get("startStopped") is not False
            or options.get("terminateExistingInstances") is not False
        ):
            raise RuntimeError("foreign/malformed managed iOS launch")
        self._check_receipts(self._guard)
        self._ready = True

    def require_scan_authority(self) -> None:
        """Check local lifetime/lease/receipts; full App/process proof is a checkpoint."""
        self.require_candidate_provenance()

    def require_scan_handoff(
        self, *, phone: IosRadioLease, run_id: str, app_id: str, deadline: float
    ) -> dict[str, str]:
        """Validate an installed, not-yet-bound owner before a scan runner takes it."""
        if (
            phone is not self._phone
            or run_id != self._run_id
            or app_id != self._bundle_id
            or deadline > self._deadline
            or self._binding_attempted
            or not self._ready
        ):
            raise RuntimeError("iOS candidate does not match the prepared scan handoff")
        self.read_candidate_sha256()
        self.require_candidate_provenance()
        return {
            "platform": "ios",
            "sha256": self._sha256,
            "evidence_directory": str(self._attempt),
        }

    @property
    def identity(self) -> ios_sdk.IosAppIdentity | None:
        """The identity this owner proved, or None before it bound successfully."""
        return self._identity

    def bind_native(
        self,
        *,
        require_owner: Callable[[], None],
        deadline: float | None = None,
    ) -> Callable[[], None]:
        """Bind once to the install/launch this run owns, without a WebDriver.

        The proof is the same native identity challenge `IosAppBinding` uses
        (`ios_sdk.read_app_identity`): the pinned pid/executable, the install and launch
        receipts, and the in-container run-id challenge, each bracketed by the caller's
        owner callback. What it cannot prove is that a *remote* driver is ours, because in
        this lane the driver runs inside the same test process; the scene receipt is what
        shows the App actually answered.

        Returns the full-tier checkpoint. A scene cannot take the cheaper per-click tier
        between its own taps, so the checkpoint belongs at scene entry and exit.
        """
        if self._binding_attempted:
            raise RuntimeError(
                "managed iOS candidate already bound or binding attempted"
            )
        self._binding_attempted = True
        if deadline is not None:
            if (
                type(deadline) not in (float, int)
                or not math.isfinite(deadline)
                or deadline > self._deadline
            ):
                raise ValueError("binding requires a finite non-extending deadline")
            self._deadline = deadline
        self.require_candidate_provenance()
        with self._checking():
            identity = ios_sdk.read_app_identity(
                device=self._device,
                run_id=self._run_id,
                bundle_id=self._bundle_id,
                install_receipt=self.install_receipt,
                launch_receipt=self.launch_receipt,
                attempt=self._attempt,
                expected_identity=None,
                require_owner=require_owner,
                cwd=self._cwd,
                deadline=self._deadline,
            )
            self._identity = identity

        def require_current(*, deadline: float | None = None) -> None:
            """Re-prove the pinned App, and let the owner poison itself if the proof moved.

            `_checking` already records any non-deadline failure as this owner's own, which
            is the contract cleanup relies on: a candidate whose identity moved must refuse
            to close a process it can no longer prove. A scene-budget expiry is not that —
            it is re-raised untouched so cleanup can still run under its own budget.
            """
            budget = (
                self._deadline if deadline is None else min(deadline, self._deadline)
            )
            with self._checking():
                # The owner's own liveness comes first: a retired candidate or a lost phone
                # lease is not a device question.
                self._guard()
                current = ios_sdk.read_app_identity(
                    device=self._device,
                    run_id=self._run_id,
                    bundle_id=self._bundle_id,
                    install_receipt=self.install_receipt,
                    launch_receipt=self.launch_receipt,
                    attempt=self._attempt,
                    expected_identity=identity,
                    require_owner=require_owner,
                    cwd=self._cwd,
                    deadline=budget,
                )
                if current != identity:
                    raise RuntimeError(
                        "iOS pinned App process/install identity changed"
                    )

        return require_current

    def finalize(
        self,
        primary_error: BaseException | None,
        *,
        deadline: float,
        release_retained: bool = False,
    ) -> None:
        """Finalize the App once, while the caller still owns the phone lease.

        Pass this to ``phone.finish_observation(finalize_owned=...)``: that owner
        records primary/cleanup errors and either releases or quarantines the lease.
        A separate absolute cleanup deadline cannot extend the scenario or restore
        invalidated provenance. Only a previously bound native boot may be checked;
        unbound/uncertain launches are quarantined, never adopted during cleanup.
        Success retains the App. Failure requests one PID termination after fresh
        identity proof and verifies absence; neither means BLeng resource-zero.
        `release_retained` terminates on success by the same verified path, for a
        caller running a sequence of scenes rather than inspecting one live result.
        """
        if self._finalization_started:
            raise RuntimeError("iOS candidate finalization is one-shot")
        if self._retired:
            raise RuntimeError("iOS candidate owner already retired")
        self._finalization_started = True
        self._retired = True
        terminate = primary_error is not None or release_retained
        evidence = self._attempt / "finalization"
        evidence.mkdir(mode=0o700)
        result: dict[str, Any] = {
            "scope": "formal-ios-candidate-finalization",
            "run_id": self._run_id,
            "action": "terminate" if terminate else "retain",
            "released_retained_app": release_retained,
            "identity": None if self._identity is None else asdict(self._identity),
            "status": "failed",
            "error": None,
        }

        def publish() -> None:
            with (evidence / "result.json").open("x", encoding="utf-8") as stream:
                json.dump(result, stream, sort_keys=True)
                stream.write("\n")

        try:
            self._finalize_app(
                evidence=evidence,
                terminate=terminate,
                deadline=deadline,
            )
        except BaseException as error:
            result["error"] = "".join(format_exception_only(error)).strip()
            try:
                publish()
            except Exception as publication:  # noqa: BLE001 -- preserve cleanup failure as well as evidence failure.
                error.add_note(f"iOS finalization evidence: {publication}")
            raise
        result["status"] = "passed"
        publish()  # Failure here must also quarantine; no evidence-free release.

    def _finalize_app(
        self, *, evidence: Path, terminate: bool, deadline: float
    ) -> None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite absolute cleanup deadline required")
        if self._failure is not None:
            raise RuntimeError("iOS candidate authority invalidated") from self._failure
        identity = self._identity
        if not self._ready:
            raise RuntimeError(
                "no verified install/launch receipt; refusing cleanup adoption"
            )

        def budget() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("iOS candidate cleanup deadline exceeded")
            return min(5.0, remaining)

        def phone_guard() -> None:
            budget()
            self._require_phone_owner()
            budget()

        def guard() -> None:
            phone_guard()
            self._check_receipts(phone_guard)

        def candidate() -> None:
            guard()
            if fingerprint(self._app, require_current=phone_guard) != self._sha256:
                raise RuntimeError("frozen iOS candidate changed before finalization")
            guard()

        def sdk(command: list[str]) -> dict[str, Any]:
            guard()
            timeout = int(budget())
            # `devicectl` itself refuses any `--timeout` below five seconds, and handing it one
            # turned a scene that had already passed into an opaque exit 64 during cleanup —
            # observed as `iOS identity SDK failed (exit=64)` after the recovery scene passed.
            # The scenario budget now reserves this window (see `run_bleng_ios_scenes`), so
            # reaching it is a real budget failure and is named as one.
            if timeout < DEVICECTL_MIN_TIMEOUT:
                raise TimeoutError(
                    "insufficient cleanup SDK deadline budget: devicectl needs at least "
                    f"{DEVICECTL_MIN_TIMEOUT}s, {timeout}s left"
                )
            if "--timeout" in command:
                command[command.index("--timeout") + 1] = str(timeout)
            else:
                command.extend(["--timeout", str(timeout)])
            receipt = Path(command[command.index("--json-output") + 1])
            if receipt.exists() or receipt.is_symlink():
                raise RuntimeError("refusing an existing cleanup receipt")
            try:
                ios_sdk.run_logged(
                    command,
                    cwd=self._cwd,
                    log=receipt.with_suffix(".log"),
                    timeout=budget(),
                )
            except Exception as error:
                try:
                    guard()
                except Exception as boundary:  # noqa: BLE001 -- retain the SDK error when ownership also fails.
                    error.add_note(f"iOS cleanup post-command boundary: {boundary}")
                raise
            guard()
            observed, _ = ios_sdk._identity_official(
                receipt, ".".join(command[1:5]), self._device, command[1:]
            )
            if observed["deviceIdentifier"] != device_identifier:
                raise RuntimeError("foreign CoreDevice cleanup receipt")
            return observed

        install_result, _ = ios_sdk._identity_official(
            self.install_receipt, "devicectl.device.install.app", self._device
        )
        device_identifier = install_result.get("deviceIdentifier")
        if type(device_identifier) is not str or not device_identifier:
            raise RuntimeError("managed iOS install receipt lacks a device identifier")

        def finalize_unbound() -> None:
            """Retire an App this run launched but never bound to a native identity.

            `bind` can fail *after* a successful install/launch — the runner that proves
            native identity may never come up — and refusing cleanup in that state left
            the App running, so every later run for this device refused takeover. A
            verified launch receipt already names the exact executable CoreDevice
            created inside a container with a unique UUID, so matching that path cannot
            adopt a foreign App and no boot identity is needed to know which process is
            ours.

            Only this run's own executable is considered. Zero matches is a success
            (already gone), several are refused as ambiguous, and the one termination is
            never retried. Nothing here may claim resource-zero or a passing bundle.
            """
            accepted, _ = ios_sdk._identity_official(
                self.launch_receipt, "devicectl.device.process.launch", self._device
            )
            process = accepted.get("process")
            if type(process) is not dict or type(process.get("executable")) is not str:
                raise RuntimeError("managed iOS launch receipt lacks an executable")
            expected = ios_sdk._identity_executable_path(
                process["executable"], file_url=True
            )

            def matching(name: str) -> list[int]:
                rows = sdk(
                    ios_sdk.devicectl_processes(self._device, evidence / name)
                ).get("runningProcesses")
                if type(rows) is not list:
                    raise RuntimeError("malformed iOS process inventory")
                found: list[int] = []
                for row in rows:
                    if (
                        type(row) is not dict
                        or type(row.get("processIdentifier")) is not int
                        or row["processIdentifier"] <= 0
                    ):
                        raise RuntimeError("malformed iOS process inventory")
                    path = ios_sdk._identity_executable_path(
                        row.get("executable"), file_url=True
                    )
                    if path == expected:
                        found.append(row["processIdentifier"])
                return found

            before = matching("unbound-processes-before.json")
            if len(before) > 1:
                raise RuntimeError("ambiguous unbound App process inventory")
            if not before or not terminate:
                candidate()
                return
            sdk(
                ios_sdk.devicectl_terminate_process(
                    self._device, before[0], evidence / "terminate.json"
                )
            )
            # A terminate receipt is not proof of exit; the inventory is the proof.
            if matching("unbound-processes-after.json"):
                raise RuntimeError("unbound App still present after termination")
            candidate()

        candidate()
        if identity is None:
            finalize_unbound()
            return
        observed = ios_sdk.read_app_identity(
            device=self._device,
            run_id=self._run_id,
            bundle_id=self._bundle_id,
            install_receipt=self.install_receipt,
            launch_receipt=self.launch_receipt,
            attempt=evidence,
            expected_identity=identity,
            require_owner=guard,
            cwd=self._cwd,
            deadline=deadline,
        )
        if observed != identity:
            raise RuntimeError("native identity changed during finalization")
        if not terminate:
            candidate()
            return
        # CoreDevice has only PID termination, not atomic boot-conditioned kill.
        # Do no whole-App hashing after the final identity/process sweep. The
        # phone lease excludes cooperating runners, not external process races.
        sdk(
            ios_sdk.devicectl_terminate_process(
                self._device, identity.pid, evidence / "terminate.json"
            )
        )
        # A successful terminate receipt is not proof of exit. Do not retry the
        # mutation or chase a replacement process; this final inventory is read-only.
        rows = sdk(
            ios_sdk.devicectl_processes(self._device, evidence / "processes-after.json")
        ).get("runningProcesses")
        if type(rows) is not list:
            raise RuntimeError("malformed cleanup process inventory")
        installed_directory = (
            ios_sdk._identity_executable_path(
                identity.executable, file_url=True
            ).rsplit("/", 1)[0]
            + "/"
        )
        seen: set[int] = set()
        for row in rows:
            if (
                type(row) is not dict
                or type(row.get("processIdentifier")) is not int
                or row["processIdentifier"] <= 0
                or row["processIdentifier"] in seen
            ):
                raise RuntimeError("malformed or ambiguous cleanup process inventory")
            seen.add(row["processIdentifier"])
            path = ios_sdk._identity_executable_path(
                row.get("executable"), file_url=True
            )
            if (
                row["processIdentifier"] == identity.pid
                or row.get("bundleIdentifier") == self._bundle_id
                or Path(path).name == self._executable
                or path.startswith(installed_directory)
            ):
                raise RuntimeError(
                    "owned PID/App still present or replaced after termination"
                )
        candidate()

    def retire(self) -> None:
        """Revoke local authority only; no process/lease/resource-zero assertion."""
        self._retired = True
