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
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only
from typing import TYPE_CHECKING

from tools.android.state_control import AndroidRadioController
from tools.android_toolchain import PACKAGES, sdk_root

from . import android_sdk

if TYPE_CHECKING:
    from appium import webdriver


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
        self._started = self._binding_attempted = self._bound = False
        self._launch_dispatched = False
        self._install_incomplete = False
        self._retired = self._finalization_started = False
        self._failure: Exception | None = None
        self._identity: android_sdk.AndroidAppIdentity | None = None
        self._uid: int | None = None
        self._sequence = 0
        self._receipts: list[tuple[Path, str]] = []

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
            if hash_apk and (
                self._apk.stat().st_size == 0
                or self._hash(self._apk, deadline) != self._digest
            ):
                raise RuntimeError("frozen Android candidate changed")
            for path, digest in self._receipts:
                if self._hash(path, deadline) != digest:
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
                    (receipt, hashlib.sha256(raw.encode()).hexdigest()),
                    (log, hashlib.sha256(output.encode()).hexdigest()),
                ]
            )

        try:
            if label == "launch":
                # Mark before SDK dispatch: timeout/interrupt is an unknown launch,
                # never evidence that no process was created.
                self._launch_dispatched = True
            result = android_sdk.run_command(
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
            android_sdk.adb_command(self._serial, *args),
            deadline,
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
                    (log, digest),
                    (receipt, hashlib.sha256(raw.encode()).hexdigest()),
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
            "badging", [str(aapt2), "dump", "badging", str(self._apk)], self._deadline
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
        installed_path = self._adb(
            "apk-path", self._deadline, "shell", "pm", "path", self._package
        )
        match = re.fullmatch(
            r"package:(/data/app/[A-Za-z0-9_./=+~-]+/base\.apk)\n", installed_path
        )
        if match is None or any(
            part in ("", ".", "..") for part in match[1].split("/")[1:]
        ):
            raise RuntimeError("missing/split/noncanonical installed APK path")
        digest = self._adb(
            "installed-hash", self._deadline, "shell", "sha256sum", match[1]
        )
        if digest != f"{self._digest}  {match[1]}\n":
            raise RuntimeError(
                "installed APK differs from frozen candidate before launch"
            )
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
        self, deadline: float, *, cleanup: bool = False
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
        )
        return identity

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

    def bind(
        self,
        driver: webdriver.Remote,
        *,
        require_owner: Callable[[], None],
        deadline: float | None = None,
    ) -> Callable[[], None]:
        from .bleng_scan_host import pin_android_app

        if self._binding_attempted:
            raise RuntimeError("Android binding is one-shot")
        self._binding_attempted = True
        if deadline is not None:
            self._remaining(deadline)
            if deadline > self._deadline:
                raise ValueError("binding cannot extend the candidate deadline")
            self._deadline = deadline
        self._guard(self._deadline)
        if self._identity is None:
            raise RuntimeError("managed Android candidate not ready")
        try:
            binding = pin_android_app(
                driver,
                phone=self._phone,
                serial=self._serial,
                run_id=self._run_id,
                app_id=self._package,
                expected_apk_sha256=self._digest,
                read_candidate_sha256=self.read_candidate_sha256,
                require_owner=require_owner,
                cwd=self._cwd,
                deadline=self._deadline,
            )
            if binding.identity != self._identity:
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
        self._receipts.append((path, hashlib.sha256(raw.encode()).hexdigest()))
        self._bound = True

        def require_app_identity() -> None:
            try:
                binding()
            except Exception as error:
                if not (
                    isinstance(error, TimeoutError)
                    and time.monotonic() >= self._deadline
                ):
                    self._failure = error
                raise

        return require_app_identity

    def finalize(self, primary_error: BaseException | None, *, deadline: float) -> None:
        """App-only callback for ``phone.finish_app_observation``; never releases it."""
        if self._finalization_started:
            raise RuntimeError("Android finalization is one-shot")
        self._finalization_started = self._retired = True
        result = {
            "action": "retain" if primary_error is None else "force-stop",
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
            self._read_identity(deadline, cleanup=True)
            if primary_error is not None:
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
