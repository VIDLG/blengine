"""One-shot, primary-user-only formal Android candidate ownership.

Root installation is explicit opt-in; no permission/radio changes, installer
fallback or implicit App takeover.
Local SDK logs are host evidence, not device attestation. Package force-stop is
not atomic with process identity checks and does not prove native resource-zero.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only

from tools import host_command
from tools.android import adb
from tools.android.state_control import AndroidRadioController
from tools.android_toolchain import PACKAGES, sdk_root

from . import android_sdk
from .evidence_files import signature
from .evidence_io import read_small


class _DeadlineExceeded(TimeoutError):
    """Budget expiry alone does not revoke separately verified cleanup authority."""


class AndroidCandidateOwner:
    """Install/launch once, bind once, finalize once under an existing phone lease.

    Only user 0 on a single-user device is admitted. A package UID may not be
    shared. Process inventory includes that UID as well as package/worker names;
    permissions and radio setup remain explicit work outside this owner.
    """

    def __init__(
        self,
        *,
        apk: Path,
        expected_sha256: str,
        package: str,
        activity: str,
        phone: AndroidRadioController,
        serial: str,
        run_id: str,
        attempt: Path,
        cwd: Path,
        deadline: float,
        test_only: bool,
        use_root: bool = False,
        driver_runs_in_app_process: bool = False,
    ) -> None:
        if (
            type(serial) is not str
            or re.fullmatch(r"[A-Za-z0-9_.:-]+", serial) is None
            or serial.lower().startswith("emulator-")
            or type(package) is not str
            or re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package
            )
            is None
            or type(activity) is not str
            or re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_$]*)+", activity
            )
            is None
            or type(expected_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or type(test_only) is not bool
            or type(use_root) is not bool
            or type(driver_runs_in_app_process) is not bool
            or type(run_id) is not str
            or str(uuid.UUID(run_id)) != run_id
        ):
            raise ValueError(
                "explicit Android device/run/APK/package/activity required"
            )
        self._remaining(deadline)
        self._apk, self._attempt, self._cwd = (
            apk.absolute(),
            attempt.absolute(),
            cwd.absolute(),
        )
        self._digest, self._package, self._activity = expected_sha256, package, activity
        self._phone, self._serial, self._run_id = phone, serial, run_id
        self._deadline, self._test_only = deadline, test_only
        self._use_root = use_root
        # An instrumented lane runs its driver in the target package's own process, so the
        # pid/kernel identity observed at launch is not the identity that may exist at
        # cleanup. In that lane ownership is proven by the installation (uid + digest)
        # instead; every other lane keeps the exact process identity.
        self._driver_runs_in_app_process = driver_runs_in_app_process
        self._started = self._binding_attempted = self._bound = False
        self._launch_dispatched = False
        self._install_incomplete = False
        self._retired = self._finalization_started = False
        self._failure: Exception | None = None
        self._identity: android_sdk.AndroidAppIdentity | None = None
        self._uid: int | None = None
        self._sequence = 0
        # (path, (identity signature, digest)): the signature lets a guard prove
        # immutability with one lstat instead of re-reading every receipt.
        self._receipts: list[tuple[Path, tuple[tuple[int, ...], str]]] = []
        # Identity the frozen APK's digest was established under; see _apk_unchanged.
        self._apk_pin: tuple[tuple[int, ...], str] | None = None

        if self._apk.suffix != ".apk":
            raise ValueError("a frozen .apk is required")
        self._guard(deadline)
        self._attempt.mkdir(mode=0o700)

    @staticmethod
    def _remaining(deadline: float) -> float:
        if type(deadline) not in (float, int) or not math.isfinite(deadline):
            raise ValueError("finite absolute monotonic deadline required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded("Android candidate deadline exceeded")
        return remaining

    def _phone_guard(self, deadline: float) -> None:
        self._remaining(deadline)
        phone = self._phone
        if (
            type(phone) is not AndroidRadioController
            or phone.device != self._serial
            or phone.run_id != self._run_id
            or phone.document.get("device") != self._serial
            or phone.document.get("run_id") != self._run_id
            or phone.document.get("actions") != []
            or phone.initial_state is not None
            or phone.last_state is not None
        ):
            raise RuntimeError("Android App-only phone/run ownership changed")
        phone.require_owner()
        self._remaining(deadline)

    def _hash(self, path: Path, deadline: float) -> str:
        self._phone_guard(deadline)
        before = path.lstat()

        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError("linked/nonregular/empty Android candidate evidence")
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        digest = hashlib.sha256()
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError("Android candidate evidence changed type")
            while chunk := stream.read(65536):
                self._phone_guard(deadline)
                digest.update(chunk)
            finished = os.fstat(stream.fileno())
        after = path.lstat()
        signatures = {
            (
                s.st_dev,
                s.st_ino,
                s.st_mode,
                s.st_nlink,
                s.st_size,
                s.st_mtime_ns,
                s.st_ctime_ns,
            )
            for s in (before, opened, finished, after)
        }
        if len(signatures) != 1:
            raise RuntimeError("Android candidate evidence changed while hashing")
        self._phone_guard(deadline)
        return digest.hexdigest()

    def _apk_unchanged(self, deadline: float) -> bool:
        """Prove the frozen APK is the authorized file without rehashing it every guard.

        The first check hashes the whole APK — the only moment its digest can be
        established — and pins the identity it was read under. Later checks compare that
        identity, which a same-content replacement cannot reproduce; rehashing would only
        re-accept such a replacement.
        """
        if self._apk.stat().st_size == 0:
            return False
        pin = self._apk_pin
        if pin is not None and signature(self._apk.lstat()) == pin[0]:
            # Unchanged identity: still the bytes authenticated below.
            return True
        actual = self._hash(self._apk, deadline)
        if actual != self._digest:
            return False
        self._apk_pin = (signature(self._apk.lstat()), actual)
        return True

    @staticmethod
    def _pin(path: Path, expected: bytes) -> tuple[tuple[int, ...], str]:
        """Pin a just-written receipt by identity and digest.

        The caller already knows the bytes it wrote; this re-reads them so the pin is
        about the file on disk, and refuses if the file is not what was written.
        """
        raw = read_small(path)
        if raw != expected:
            raise RuntimeError(
                "Android candidate receipt differs from what was written"
            )
        return signature(path.lstat()), hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _pin_current(path: Path, pin: tuple[tuple[int, ...], str]) -> bool:
        """A published receipt is immutable by identity, like every other receipt."""
        return signature(path.lstat()) == pin[0]

    def _guard(
        self, deadline: float, *, cleanup: bool = False, hash_apk: bool = True
    ) -> None:
        if self._failure is not None:
            raise RuntimeError(
                "Android candidate authority invalidated"
            ) from self._failure
        try:
            if self._retired and not cleanup:
                raise RuntimeError("Android candidate owner retired")
            self._phone_guard(deadline)
            if hash_apk and not self._apk_unchanged(deadline):
                raise RuntimeError("frozen Android candidate changed")
            for path, pin in self._receipts:
                if not self._pin_current(path, pin):
                    raise RuntimeError("Android managed receipt changed")
        except _DeadlineExceeded:
            raise
        except Exception as error:
            self._failure = error
            raise

    def read_candidate_sha256(self) -> str:
        self._guard(self._deadline)
        return self._digest

    def _command(
        self,
        label: str,
        argv: list[str],
        deadline: float,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]],
        cleanup: bool = False,
        hash_apk: bool = True,
    ) -> str:
        guard = lambda: self._guard(deadline, cleanup=cleanup, hash_apk=hash_apk)
        guard()
        self._sequence += 1
        stem = self._attempt / f"{self._sequence:03d}-{label}"
        log, receipt = stem.with_suffix(".log"), stem.with_suffix(".json")
        record = {
            "argv": argv,
            "run_id": self._run_id,
            "returncode": None,
            "error": None,
        }
        output = ""

        def publish() -> None:
            # Empty stdout is valid; the JSON receipt also binds its raw digest.
            with log.open("x", encoding="utf-8") as stream:
                stream.write(output)
            record["stdout_sha256"] = hashlib.sha256(output.encode()).hexdigest()
            raw = json.dumps(record, sort_keys=True) + "\n"
            with receipt.open("x", encoding="utf-8") as stream:
                stream.write(raw)
            self._receipts.extend(
                [
                    (receipt, self._pin(receipt, raw.encode())),
                    (log, self._pin(log, output.encode())),
                ]
            )

        try:
            if label == "launch":
                # Mark before SDK dispatch: timeout/interrupt is an unknown launch,
                # never evidence that no process was created.
                self._launch_dispatched = True
            result = runner(
                argv,
                cwd=self._cwd,
                timeout=min(30.0, self._remaining(deadline)),
                check=False,
            )
            output, record["returncode"] = result.stdout, result.returncode
        except BaseException as error:
            record["error"] = "".join(format_exception_only(error)).strip()
            cause_output = getattr(error.__cause__, "stdout", "") or ""
            output = (
                cause_output.decode(errors="replace")
                if isinstance(cause_output, bytes)
                else str(cause_output)
            )
            try:
                publish()
            except Exception as publication:  # noqa: BLE001 -- retain the primary SDK failure.
                error.add_note(f"Android command evidence: {publication}")
            try:
                guard()
            except Exception as boundary:  # noqa: BLE001 -- retain both failures.
                error.add_note(f"Android post-command boundary: {boundary}")
            if isinstance(error, android_sdk.TRANSPORT_FAILURES):
                raise android_sdk.sealed_failure(
                    f"Android {label} command failed", error
                ) from error
            raise
        if result.returncode:
            error = android_sdk.AndroidSdkError(
                f"Android {label} failed (exit={result.returncode}); log={log}"
            )
            try:
                publish()
            except Exception as publication:  # noqa: BLE001 -- SDK rejection remains primary.
                error.add_note(f"Android command evidence: {publication}")
            try:
                guard()
            except Exception as boundary:  # noqa: BLE001 -- preserve SDK rejection.
                error.add_note(f"Android post-command boundary: {boundary}")
            raise error
        publish()
        guard()
        if len(output.encode()) > 1024 * 1024:
            raise RuntimeError("oversized Android SDK output")
        return output

    def _adb(
        self,
        label: str,
        deadline: float,
        *args: str,
        cleanup: bool = False,
        hash_apk: bool = True,
    ) -> str:
        return self._command(
            label,
            adb.adb_command(self._serial, *args),
            deadline,
            runner=adb.run_command,
            cleanup=cleanup,
            hash_apk=hash_apk,
        )

    def _user_and_uid(self, deadline: float, *, cleanup: bool = False) -> int | None:
        if (
            self._adb(
                "current-user",
                deadline,
                "shell",
                "am",
                "get-current-user",
                cleanup=cleanup,
            ).strip()
            != "0"
        ):
            raise RuntimeError("formal Android candidate supports only current user 0")
        users = self._adb(
            "users", deadline, "shell", "pm", "list", "users", cleanup=cleanup
        )
        lines = users.splitlines()
        if (
            not lines
            or lines[0].strip() != "Users:"
            or len(lines) != 2
            or re.fullmatch(r"\s*UserInfo\{0:[^\n{}]+\}(?: running)?\s*", lines[1])
            is None
        ):
            raise RuntimeError("formal Android candidate requires a single user 0")
        output = self._adb(
            "package-uids",
            deadline,
            "shell",
            "pm",
            "list",
            "packages",
            "-U",
            "--user",
            "0",
            cleanup=cleanup,
        )
        packages: dict[str, int] = {}
        for line in output.splitlines():
            match = re.fullmatch(r"package:([A-Za-z0-9_.]+) uid:([0-9]+)", line)
            if match is None or match[1] in packages:
                raise RuntimeError("malformed package UID inventory")
            packages[match[1]] = int(match[2])
        uid = packages.get(self._package)
        if uid is not None and (
            not 10000 <= uid < 100000 or list(packages.values()).count(uid) != 1
        ):
            raise RuntimeError("shared UID or non-primary App UID is unsupported")
        return uid

    def _require_absent(
        self, deadline: float, uid: int | None, *, cleanup: bool = False
    ) -> None:
        output = self._adb(
            "processes",
            deadline,
            "shell",
            "ps",
            "-A",
            "-o",
            "PID,UID,ARGS",
            cleanup=cleanup,
        )
        lines = output.splitlines()
        if not lines or lines[0].split() != ["PID", "UID", "ARGS"]:
            raise RuntimeError("malformed process inventory header")
        seen: set[int] = set()
        for line in lines[1:]:
            fields = line.split(maxsplit=2)
            if (
                len(fields) != 3
                or not fields[0].isdigit()
                or int(fields[0]) <= 0
                or not fields[1].isdigit()
                or int(fields[0]) in seen
            ):
                raise RuntimeError("malformed/ambiguous Android process inventory")
            seen.add(int(fields[0]))
            name = fields[2].split()[0]
            if (
                int(fields[1]) == uid
                or name == self._package
                or name.startswith(self._package + ":")
            ):
                raise RuntimeError(
                    "Android App/UID already running; refusing takeover or incomplete cleanup"
                )

    def _require_installed_apk(
        self, deadline: float, *, cleanup: bool = False, detail: str = ""
    ) -> str:
        """Prove the installed base APK is still this frozen candidate's."""
        installed_path = self._adb(
            "apk-path", deadline, "shell", "pm", "path", self._package, cleanup=cleanup
        )
        match = re.fullmatch(
            r"package:(/data/app/[A-Za-z0-9_./=+~-]+/base\.apk)\n", installed_path
        )
        if match is None or any(
            part in ("", ".", "..") for part in match[1].split("/")[1:]
        ):
            raise RuntimeError("missing/split/noncanonical installed APK path")
        digest = self._adb(
            "installed-hash", deadline, "shell", "sha256sum", match[1], cleanup=cleanup
        )
        if digest != f"{self._digest}  {match[1]}\n":
            raise RuntimeError(
                f"installed APK differs from the frozen candidate{detail}"
            )
        return match[1]

    def _install(self) -> None:
        """Freeze the shared installer log and outcome, including failed attempts."""
        guard = lambda: self._guard(self._deadline)
        guard()
        self._sequence += 1
        stem = self._attempt / f"{self._sequence:03d}-install"
        log, receipt = stem.with_suffix(".log"), stem.with_suffix(".json")
        record = {
            "run_id": self._run_id,
            "serial": self._serial,
            "apk_sha256": self._digest,
            "use_root": self._use_root,
            "test_only": self._test_only,
            "grant": False,
            "status": "failed",
            "error": None,
        }

        def publish() -> None:
            raw_log = log.read_bytes()
            digest = hashlib.sha256(raw_log).hexdigest()
            record["stdout_sha256"] = digest
            raw = json.dumps(record, sort_keys=True) + "\n"
            with receipt.open("x", encoding="utf-8") as stream:
                stream.write(raw)
            self._receipts.extend(
                [
                    (log, self._pin(log, raw_log)),
                    (receipt, self._pin(receipt, raw.encode())),
                ]
            )

        try:
            with log.open("x", encoding="utf-8") as stream:
                android_sdk.install_apk(
                    self._serial,
                    self._apk,
                    log=stream,
                    test_only=self._test_only,
                    use_root=self._use_root,
                    grant=False,
                    deadline=self._deadline,
                    require_owner=guard,
                    cwd=self._cwd,
                )
            record["status"] = "passed"
        except BaseException as error:
            record["error"] = "".join(format_exception_only(error)).strip()
            try:
                publish()
            except Exception as publication:  # noqa: BLE001 -- keep the install failure primary.
                error.add_note(f"Android install evidence: {publication}")
            try:
                guard()
            except Exception as boundary:  # noqa: BLE001 -- retain both failures.
                error.add_note(f"Android post-install boundary: {boundary}")
            raise
        publish()
        guard()

    def install_and_launch(self) -> None:
        if self._started:
            raise RuntimeError("Android install/launch is one-shot")
        self._started = True
        build_tools = next(p for p in PACKAGES if p.package.startswith("build-tools/"))
        aapt2 = sdk_root() / build_tools.directory / build_tools.required_files[0]
        badging = self._command(
            "badging",
            [str(aapt2), "dump", "badging", str(self._apk)],
            self._deadline,
            runner=host_command.run,
        )
        if re.findall(r"(?m)^package: name='([^']+)'", badging) != [
            self._package
        ] or re.findall(r"(?m)^launchable-activity: name='([^']+)'", badging) != [
            self._activity
        ]:
            raise RuntimeError(
                "frozen APK identity does not match expected package/activity"
            )
        manifest = self._command(
            "manifest",
            [
                str(aapt2),
                "dump",
                "xmltree",
                "--file",
                "AndroidManifest.xml",
                str(self._apk),
            ],
            self._deadline,
            runner=host_command.run,
        )
        if (
            len(re.findall(r"(?m)^\s*E: manifest(?:\s|$)", manifest)) != 1
            or "sharedUserId" in manifest
        ):
            raise RuntimeError(
                "APK manifest missing or shared UID declaration unsupported"
            )
        if self._adb("device-state", self._deadline, "get-state").strip() != "device":
            raise RuntimeError("explicit Android device is unavailable")
        self._require_absent(self._deadline, self._user_and_uid(self._deadline))
        self._install_incomplete = True
        self._install()
        self._install_incomplete = False
        self._uid = self._user_and_uid(self._deadline)
        if self._uid is None:
            raise RuntimeError("installed Android package UID is missing")
        self._require_installed_apk(self._deadline, detail=" before launch")
        self._require_absent(self._deadline, self._uid)
        launched = self._adb(
            "launch",
            self._deadline,
            "shell",
            "am",
            "start",
            "-W",
            "--user",
            "0",
            "-n",
            self._package + "/" + self._activity,
        )
        activities = re.findall(r"(?m)^Activity: (\S+)$", launched)
        valid_components = {self._package + "/" + self._activity}
        if self._activity.startswith(self._package + "."):
            valid_components.add(
                self._package + "/" + self._activity[len(self._package) :]
            )
        if (
            re.findall(r"(?m)^Status: (.*)$", launched) != ["ok"]
            or re.findall(r"(?m)^LaunchState: (.*)$", launched) != ["COLD"]
            or len(activities) != 1
            or activities[0] not in valid_components
            or launched.splitlines()[-1:] != ["Complete"]
            or re.search(r"(?im)^(?:Error|Warning):", launched)
        ):
            raise RuntimeError("expected an exact fresh Android cold launch receipt")
        self._identity = self._read_identity(self._deadline)
        if self._identity.uid != self._uid:
            self._identity = None
            raise RuntimeError("launched Android identity has a foreign UID")

    def _read_identity(
        self,
        deadline: float,
        *,
        cleanup: bool = False,
        record_command: Callable[[dict], None] | None = None,
    ) -> android_sdk.AndroidAppIdentity:
        identity = android_sdk.read_app_identity(
            serial=self._serial,
            package=self._package,
            expected_apk_sha256=self._digest,
            expected_identity=self._identity,
            cwd=self._cwd,
            deadline=deadline,
            require_owner=lambda: self._guard(
                deadline, cleanup=cleanup, hash_apk=not cleanup
            ),
            **(
                {"record_command": record_command} if record_command is not None else {}
            ),
        )
        return identity

    def inspect_running_identity(
        self,
        *,
        deadline: float,
        record_command: Callable[[dict], None] | None = None,
    ) -> android_sdk.AndroidAppIdentity:
        """Fresh read-only proof of this owner's original launch, without a driver.

        This checkpoint never adopts/replaces an identity, binds automation or
        extends the candidate deadline. Callers still own UI/session authority.
        Budget expiry alone preserves independently timed, original-identity
        cleanup; other identity/transport/ownership failures invalidate authority.
        """
        self._remaining(deadline)
        if deadline > self._deadline:
            raise ValueError("identity checkpoint cannot extend the candidate deadline")
        self._guard(deadline)
        if self._identity is None:
            raise RuntimeError("managed Android candidate not ready")
        try:
            return self._read_identity(deadline, record_command=record_command)
        except Exception as error:
            timed_out = isinstance(error, TimeoutError) or (
                isinstance(error, android_sdk.AndroidSdkError)
                and isinstance(error.__cause__, subprocess.TimeoutExpired)
            )
            if not (timed_out and time.monotonic() >= deadline):
                self._failure = self._failure or error
            raise

    def require_scan_authority(self) -> None:
        """Check local lifetime/lease/receipts; full APK/process proof is a checkpoint."""
        self._guard(self._deadline, hash_apk=False)

    def require_scan_handoff(
        self,
        *,
        phone: AndroidRadioController,
        run_id: str,
        app_id: str,
        deadline: float,
    ) -> dict[str, str]:
        """Validate an installed, not-yet-bound owner before a scan runner takes it."""
        if (
            phone is not self._phone
            or run_id != self._run_id
            or app_id != self._package
            or deadline > self._deadline
            or self._binding_attempted
            or self._identity is None
        ):
            raise RuntimeError(
                "Android candidate does not match the prepared scan handoff"
            )
        self._guard(deadline)
        return {
            "platform": "android",
            "sha256": self._digest,
            "evidence_directory": str(self._attempt),
        }

    @property
    def identity(self) -> android_sdk.AndroidAppIdentity | None:
        """The identity this owner proved, or None before it launched/bound."""
        return self._identity

    def bind_native(
        self,
        *,
        require_owner: Callable[[], None],
        deadline: float | None = None,
    ) -> Callable[[], None]:
        """Bind once to the install/launch this run owns, without a WebDriver.

        The proof is the same OS identity the WebDriver binding reads (pinned pid, uid,
        start ticks, installed base APK). What it cannot prove is that a *remote* driver is
        ours, because in this lane the driver runs inside the instrumentation process and
        the device reports both; the scene receipt is what shows the App actually answered.
        """
        if self._binding_attempted:
            raise RuntimeError("Android binding is one-shot")
        if deadline is not None:
            self._remaining(deadline)
            if deadline > self._deadline:
                raise ValueError("binding cannot extend the candidate deadline")
            self._deadline = deadline
        self._binding_attempted = True
        self._guard(self._deadline)
        if self._identity is None:
            raise RuntimeError("managed Android candidate not ready")
        if not self._driver_runs_in_app_process:
            raise RuntimeError(
                "bind_native is only meaningful when the lane's driver runs in the App's "
                "own process"
            )
        try:
            identity = self._read_identity(self._deadline)
            if identity != self._identity:
                raise RuntimeError(
                    "Android process identity changed before driver binding"
                )
        except Exception as error:
            self._failure = error
            raise
        path = self._attempt / "bound-identity.json"
        raw = json.dumps(asdict(self._identity), sort_keys=True) + "\n"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(raw)
        self._receipts.append((path, self._pin(path, raw.encode())))
        self._bound = True

        def require_current(*, deadline: float | None = None) -> None:
            """Re-prove the installation this run owns, and poison the candidate if it moved.

            A failed check is not a transient observation: an identity that changed under a
            bound candidate means the process or package this run took is no longer provably
            its own. Cleanup must then refuse rather than adopt whatever is there now, which
            is why the failure is recorded here — a checker that only raised would let
            finalization stop a stranger's process.
            """
            budget = (
                self._deadline if deadline is None else min(deadline, self._deadline)
            )
            try:
                self._guard(budget)
                # The process may have been replaced by this lane's own instrumented run,
                # which executes inside the target package; the installation is what must be
                # ours.
                if self._user_and_uid(budget) != self._uid:
                    raise RuntimeError("Android package UID changed during the scene")
                self._require_installed_apk(budget, detail=" during the scene")
            except Exception as error:
                # The SDK preserves subprocess timeouts as AndroidSdkError causes. Budget
                # expiry alone must still allow fresh, pinned cleanup checks.
                timed_out = isinstance(error, TimeoutError) or (
                    isinstance(error, android_sdk.AndroidSdkError)
                    and isinstance(error.__cause__, subprocess.TimeoutExpired)
                )
                if not (timed_out and time.monotonic() >= budget):
                    self._failure = error
                raise

        return require_current

    def finalize(
        self,
        primary_error: BaseException | None,
        *,
        deadline: float,
        release_retained: bool = False,
    ) -> None:
        """App-only callback for ``phone.finish_app_observation``; never releases it.

        A passing run keeps the App it launched running, so an operator can look at the
        live UI and its final page. That is the default because the evidence is also the
        screen. A sequence of scenes needs the opposite: `release_retained` stops the
        exact installation this run launched, by the same verified path a failed run
        takes, so the next scene sees a device with nothing of ours running instead of
        refusing takeover. It is opt-in because stopping the App is an action, and a run
        should only take actions it was authorised to take.

        A lane whose driver runs inside the App's own process cannot retain that process:
        the instrumentation host owns it and ends it. That lane always stops the package,
        and proves ownership by the installation (uid + installed digest) rather than by
        the pid observed at launch.
        """
        if self._finalization_started:
            raise RuntimeError("Android finalization is one-shot")
        if self._driver_runs_in_app_process and not release_retained:
            raise RuntimeError(
                "the in-process lane cannot retain an App across its own test process"
            )
        self._finalization_started = self._retired = True
        stop = primary_error is not None or release_retained
        result = {
            "action": "force-stop" if stop else "retain",
            "released_retained_app": release_retained,
            "run_id": self._run_id,
            "status": "failed",
            "error": None,
            "identity": None if self._identity is None else asdict(self._identity),
        }
        try:
            self._guard(deadline, cleanup=True)
            if self._started and not self._launch_dispatched:
                if self._install_incomplete:
                    raise RuntimeError(
                        "Android installation outcome uncertain; refusing cleanup success"
                    )
                # No native launch was submitted. Installation rejection does not
                # authorize adopting or stopping any process, even if one exists.
                result.update(action="no-launch", status="passed")
                self._publish_finalization(result)
                return
            # Exact cold launch plus the original OS identity establishes App
            # ownership even if automation fails before driver binding. The guard
            # above verifies immutable launch evidence; the fresh read below must
            # still match that original identity, never adopt a replacement.
            if self._identity is None:
                raise RuntimeError(
                    "no launched Android identity; refusing cleanup adoption"
                )
            if self._user_and_uid(deadline, cleanup=True) != self._uid:
                raise RuntimeError("Android package UID changed before cleanup")
            if self._driver_runs_in_app_process:
                # The instrumented run replaced the launched process with its own, so the
                # installation is the proof: same UID, same installed base APK digest.
                self._require_installed_apk(
                    deadline, cleanup=True, detail=" before cleanup"
                )
            else:
                self._read_identity(deadline, cleanup=True)
            if stop:
                # Package-wide stop is not atomic with the preceding process check.
                # Avoid whole-APK hashing between the final snapshot and mutation.
                self._adb(
                    "force-stop",
                    deadline,
                    "shell",
                    "am",
                    "force-stop",
                    "--user",
                    "0",
                    self._package,
                    cleanup=True,
                    hash_apk=False,
                )
                self._require_absent(deadline, self._uid, cleanup=True)
            self._guard(deadline, cleanup=True)
            result["status"] = "passed"
        except BaseException as error:
            result["error"] = "".join(format_exception_only(error)).strip()
            try:
                self._publish_finalization(result)
            except Exception as publication:  # noqa: BLE001 -- preserve both cleanup errors.
                error.add_note(f"Android finalization evidence: {publication}")
            raise
        self._publish_finalization(result)

    def _publish_finalization(self, result: dict) -> None:
        with (self._attempt / "finalization.json").open(
            "x", encoding="utf-8"
        ) as stream:
            json.dump(result, stream, sort_keys=True)
            stream.write("\n")
