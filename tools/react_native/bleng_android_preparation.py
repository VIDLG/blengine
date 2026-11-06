"""Explicit, one-shot helper-only preparation for the current rooted Android host.

This is not ordinary-run provisioning or session authority. The trusted host
serializes calls and obtains its actual AndroidAutomationOwner's restoration
approval before restore, retaining the phone lease on any quarantine. No method
releases that lease.
Observations are bracketed cooperative checks, not atomic OS attestation.
"""

from __future__ import annotations

import math
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path

from tools.android.automation_observation import (
    parse_active_instrumentation,
    require_no_settings_services,
)
from tools.android.state_control import AndroidRadioController

from . import android_sdk
from .bleng_android_automation import (
    MAX_OUTPUT_BYTES,
    SETTINGS_PACKAGE,
    _collect_prepared_android_automation,
    parse_mock_location_scopes,
    require_mock_location_restored,
)
from .bleng_android_session import (
    MAX_PROCESSES_OUTPUT_BYTES,
    AndroidAutomationOwner,
)
from .capture_io import publish_new_json

_STORE = "/data/local/tmp/mock_apps.json"
_BYTES = '["io.appium.settings"]\n'
_ANDROID_SHELL_ID = 2000  # Android shell UID and GID.
_STAT = f"stat -c '%d:%i:%f:%h:%s:%u:%g' {_STORE}"
_READ_STORE = (
    f"if [ -L {_STORE} ]; then exit 41; elif [ -e {_STORE} ]; then "
    f"{_STAT} && cat {_STORE}; else printf 'absent\\n'; fi"
)
_CREATE_STORE = (
    "(set -C; umask 077; printf '%s\\n' '[\"io.appium.settings\"]' "
    f"> {_STORE}) && {_STAT} && cat {_STORE}"
)
_APPOPS = ("shell", "cmd", "appops", "get", SETTINGS_PACKAGE, "android:mock_location")


class AndroidHelperPreparation:
    """Own only one temporary package AppOp and one exclusively created store.

    ``authorization`` must be exactly True for this run's actual App-only phone
    controller. ``deadline`` is the scenario's absolute monotonic deadline.
    ``restore`` takes a separate absolute cleanup deadline, never an extension of
    scenario authority. Successful prepare requires an actual automation owner's
    public restoration guard to prove closure or no session submission. Failed
    prepare permits explicit rollback without one, but still refuses unknown
    sessions/processes via live inventory.

    A mutation with no completion receipt is never retried or reported restored.
    Safe, attributable revocation is still attempted; an unpinned store is never
    adopted by matching contents. The host must preserve the lease/quarantine.
    """

    def __init__(
        self,
        *,
        phone: AndroidRadioController,
        run_id: str,
        authorization: bool,
        appium_home: Path,
        expected_apk_sha256: dict[str, str],
        system_port: int,
        output: Path,
        cwd: Path,
        deadline: float,
    ) -> None:
        if authorization is not True:
            raise ValueError(
                "explicit helper AppOps/store preparation authorization required"
            )
        if type(phone) is not AndroidRadioController or phone.run_id != run_id:
            raise ValueError("same-run actual Android phone lease required")
        if (
            type(phone.device) is not str
            or re.fullmatch(r"[A-Za-z0-9_.:-]+", phone.device) is None
            or phone.device.startswith("emulator-")
        ):
            raise ValueError("explicit canonical physical Android phone required")
        if not all(isinstance(p, Path) for p in (appium_home, output, cwd)):
            raise TypeError("explicit preparation paths required")
        self._phone, self._run_id, self._device = phone, run_id, phone.device
        self._home, self._expected = appium_home.absolute(), dict(expected_apk_sha256)
        self._port, self._output, self._cwd = system_port, output.absolute(), cwd
        self._deadline = deadline
        self._phase = "new"
        self._failure: BaseException | None = None
        self._restore_failure: BaseException | None = None
        self._evidence_failure: BaseException | None = None
        self._baseline: dict | None = None
        self._before: str | None = None
        self._pin: str | None = None
        self._attempts: set[str] = set()
        self._unknown: set[str] = set()
        self._uncertain_publications: set[str] = set()
        self._records: dict[Path, tuple[int, int, str]] = {}
        self._sequence = 0
        self._phone_guard(deadline)
        self._output.mkdir(mode=0o700)
        info = self._output.lstat()
        self._output_identity = (info.st_dev, info.st_ino)

    @staticmethod
    def _remaining(deadline: float) -> float:
        if type(deadline) not in (float, int) or not math.isfinite(deadline):
            raise ValueError("finite absolute preparation deadline required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Android helper preparation deadline exceeded")
        return remaining

    def _phone_guard(self, deadline: float) -> None:
        self._remaining(deadline)
        phone = self._phone
        if (
            phone.device != self._device
            or phone.run_id != self._run_id
            or phone.document.get("device") != self._device
            or phone.document.get("run_id") != self._run_id
            or phone.document.get("actions") != []
            or phone.initial_state is not None
            or phone.last_state is not None
        ):
            raise RuntimeError("Android helper phone/run ownership changed")
        phone.require_owner()
        self._remaining(deadline)

    def _remember(self, path: Path, digest: str | None = None) -> None:
        try:
            actual = AndroidAutomationOwner._record_digest(path)
            if digest is not None and digest != actual:
                raise RuntimeError("Android helper evidence changed during publication")
            info = path.lstat()
            self._records[path] = (info.st_dev, info.st_ino, actual)
        except Exception as error:
            self._evidence_failure = error
            raise

    def _records_current(self) -> None:
        info = self._output.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != self._output_identity
        ):
            raise RuntimeError("Android helper evidence directory changed")
        for path, expected in self._records.items():
            info = path.lstat()
            current = (
                info.st_dev,
                info.st_ino,
                AndroidAutomationOwner._record_digest(path),
            )
            if current != expected:
                raise RuntimeError("Android helper evidence changed")

    def _guard(self, deadline: float) -> None:
        self._phone_guard(deadline)
        if self._evidence_failure is not None:
            raise RuntimeError(
                "Android helper evidence permanently invalidated"
            ) from self._evidence_failure
        try:
            self._records_current()
        except Exception as error:
            self._evidence_failure = error
            raise
        self._phone_guard(deadline)

    def _publish(
        self, name: str, value: dict, *, deadline: float | None = None
    ) -> None:
        path = self._output / name
        published: list[tuple[int, int]] = []
        try:
            digest = publish_new_json(path, value, on_commit=published.append)
        except BaseException:
            if published:
                # A committed path without a returned digest cannot be adopted
                # as immutable evidence. Still permit safe device rollback.
                self._uncertain_publications.add(name)
            raise
        if deadline is None:
            self._remember(path, digest)
            return
        try:
            self._remember(path, digest)
            self._guard(deadline)
        except BaseException as error:
            try:
                info = path.lstat()
                if (
                    published != [(info.st_dev, info.st_ino)]
                    or AndroidAutomationOwner._record_digest(path) != digest
                ):
                    raise RuntimeError(
                        "helper terminal receipt changed; refusing retraction"
                    )
                current = path.lstat()
                if (current.st_dev, current.st_ino) != published[0]:
                    raise RuntimeError("helper terminal receipt identity changed")
                path.unlink()
                self._records.pop(path, None)
            except BaseException as retraction:  # noqa: BLE001 -- preserve late authority failure.
                error.add_note(f"helper terminal receipt retraction: {retraction}")
            raise

    def _command(
        self,
        deadline: float,
        *args: str,
        mutation: str | None = None,
        observe: Callable[[str], None] | None = None,
    ) -> str:
        self._guard(deadline)
        argv = android_sdk.adb_command(self._device, *args)
        self._guard(deadline)
        self._sequence += 1
        record = {"argv": argv, "stdout": "", "returncode": None, "error": None}
        primary: BaseException | None = None
        if mutation is not None:
            # Durable intent precedes SDK dispatch; memory tracks uncertainty even
            # if the process/transport or receipt publication subsequently fails.
            self._publish(
                f"{self._sequence:03d}-attempt.json",
                {"mutation": mutation, "run_id": self._run_id},
            )
            self._guard(deadline)
            self._attempts.add(mutation)
            self._unknown.add(mutation)
        try:
            result = android_sdk.run_command(
                argv,
                cwd=self._cwd,
                timeout=min(5.0, self._remaining(deadline)),
                check=False,
            )
            record["returncode"] = result.returncode
            limit = (
                MAX_PROCESSES_OUTPUT_BYTES
                if args == ("shell", "dumpsys", "activity", "processes")
                else MAX_OUTPUT_BYTES
            )
            if type(result.stdout) is not str or len(result.stdout.encode()) >= limit:
                raise android_sdk.AndroidSdkError(
                    "invalid/oversized preparation SDK output"
                )
            record["stdout"] = result.stdout
            if result.returncode:
                raise android_sdk.AndroidSdkError(
                    f"helper command failed (exit={result.returncode}): {result.stdout[-1500:]}"
                )
            if observe is not None:
                observe(result.stdout)  # Pin before publication can fail.
            if mutation is not None:
                self._unknown.remove(mutation)
        except BaseException as error:  # noqa: BLE001 -- retain interruption/SDK cause with receipts.
            primary = error
        try:
            self._guard(deadline)
        except BaseException as boundary:  # noqa: BLE001 -- preserve the original dispatch error.
            if primary is None:
                primary = boundary
            else:
                primary.add_note(f"helper post-command boundary: {boundary}")
        if primary is not None:
            record["error"] = str(primary)
        try:
            self._publish(f"{self._sequence:03d}-command.json", record)
        except BaseException as publication:
            if primary is None:
                raise
            primary.add_note(f"helper command publication: {publication}")
        if primary is not None:
            raise primary
        return result.stdout

    def _inventory(self, label: str, deadline: float) -> dict:
        self._guard(deadline)
        output = self._output / label
        primary: BaseException | None = None
        try:
            _, data = _collect_prepared_android_automation(
                phone=self._phone,
                appium_home=self._home,
                expected_apk_sha256=self._expected,
                system_port=self._port,
                output=output,
                cwd=self._cwd,
                deadline=deadline,
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                for path in output.glob("*.json"):
                    self._remember(path)
            except BaseException as evidence:
                if primary is None:
                    raise
                primary.add_note(f"helper inventory evidence: {evidence}")
        self._guard(deadline)
        if self._baseline is not None:
            for key in (
                "boot_id",
                "helpers",
                "settings_processes",
                "forwardings",
                "observed_hidden_api_policy",
            ):
                if data[key] != self._baseline[key]:
                    raise RuntimeError(f"Android helper baseline changed: {key}")
        return data

    def _require_idle(self, deadline: float) -> None:
        if parse_active_instrumentation(
            self._command(deadline, "shell", "dumpsys", "activity", "processes")
        ):
            raise RuntimeError("unknown active Android instrumentation")
        require_no_settings_services(
            self._command(
                deadline, "shell", "dumpsys", "activity", "services", SETTINGS_PACKAGE
            )
        )

    @staticmethod
    def _store_pin(raw: str) -> str:
        identity, separator, content = raw.partition("\n")
        match = re.fullmatch(
            rf"([0-9]+):([0-9]+):([0-9a-f]+):1:{len(_BYTES.encode())}:"
            rf"{_ANDROID_SHELL_ID}:{_ANDROID_SHELL_ID}",
            identity,
        )
        if (
            not separator
            or match is None
            or int(match[3], 16) != (stat.S_IFREG | 0o600)
            or int(match[1]) <= 0
            or int(match[2]) <= 0
            or content != _BYTES
        ):
            raise RuntimeError("unknown/nonregular helper store identity or bytes")
        return identity

    def _pin_created_store(self, raw: str) -> None:
        self._pin = self._store_pin(raw)

    def _set_op(self, deadline: float, mode: str) -> None:
        # Only these fixed commands are ever executed as root; no UID/global reset.
        if mode not in {"allow", "deny"}:
            raise ValueError("unsupported helper operation")
        self._command(
            deadline,
            "shell",
            f"su -c 'cmd appops set --user 0 {SETTINGS_PACKAGE} android:mock_location {mode}'",
            mutation=f"appops-{mode}",
        )

    def prepare(self) -> None:
        if self._phase != "new":
            raise RuntimeError("Android helper prepare is one-shot")
        self._phase = "preparing"
        try:
            data = self._inventory("before", self._deadline)
            self._require_idle(self._deadline)
            if set(data["observed_hidden_api_policy"].values()) != {"null"}:
                raise RuntimeError(
                    "helper preparation requires default hidden-API baseline"
                )
            scopes = data["observed_mock_location_scopes"]
            if scopes not in ({"default": "deny"}, {"package": "deny"}):
                raise RuntimeError(
                    "helper preparation requires default/package deny baseline"
                )
            self._baseline = data
            self._before = self._command(self._deadline, *_APPOPS)
            if parse_mock_location_scopes(self._before) != scopes:
                raise RuntimeError("helper AppOps baseline changed")
            if self._command(self._deadline, "shell", _READ_STORE) != "absent\n":
                raise RuntimeError("unknown existing helper store; refusing overwrite")
            self._set_op(self._deadline, "allow")
            if parse_mock_location_scopes(self._command(self._deadline, *_APPOPS)) != {
                "package": "allow"
            }:
                raise RuntimeError("helper AppOps allow not confirmed")
            self._command(
                self._deadline,
                "shell",
                _CREATE_STORE,
                mutation="store-create",
                observe=self._pin_created_store,
            )
            prepared = self._inventory("prepared-inventory", self._deadline)
            self._require_idle(self._deadline)
            if prepared["observed_mock_location_scopes"] != {"package": "allow"}:
                raise RuntimeError("prepared helper AppOps changed")
            if (
                self._command(self._deadline, "shell", _READ_STORE)
                != self._pin + "\n" + _BYTES
            ):
                raise RuntimeError("prepared helper store changed")
            self._publish(
                "prepared.json",
                {
                    "status": "prepared",
                    "run_id": self._run_id,
                    "before": self._before,
                    "store_identity": self._pin,
                },
                deadline=self._deadline,
            )
            self._guard(self._deadline)
            self._phase = "prepared"
        except BaseException as error:
            self._failure, self._phase = error, "prepare-failed"
            self._report_failure("prepare-failed.json", error)
            raise

    def _report_failure(self, name: str, error: BaseException) -> None:
        try:
            self._publish(
                name,
                {
                    "status": "quarantined",
                    "error": str(error),
                    "run_id": self._run_id,
                    "attempts": sorted(self._attempts),
                    "unknown_mutations": sorted(self._unknown),
                    "uncertain_publications": sorted(self._uncertain_publications),
                    "preparation_error": None
                    if self._failure is None
                    else str(self._failure),
                    "before": self._before,
                    "store_identity": self._pin,
                },
            )
        except BaseException as publication:  # noqa: BLE001 -- failure publication must not swallow primary.
            error.add_note(f"helper failure publication: {publication}")

    def require_prepared(
        self, *, phone: AndroidRadioController, run_id: str, deadline: float
    ) -> None:
        if self._phase != "prepared" or self._failure is not None:
            raise RuntimeError(
                "Android helper preparation is not usable"
            ) from self._failure
        if (
            phone is not self._phone
            or run_id != self._run_id
            or deadline > self._deadline
        ):
            raise ValueError("same phone/run and unextended scenario deadline required")
        try:
            self._guard(deadline)
        except TimeoutError:
            raise
        except Exception as error:
            self._failure = error
            raise

    def require_handoff(
        self,
        *,
        automation: AndroidAutomationOwner,
        phone: AndroidRadioController,
        run_id: str,
        deadline: float,
    ) -> None:
        """Verify the prepared scope during use or before independently timed cleanup.

        This only binds ownership and immutable evidence; it never claims that
        the platform owner is closed or that a session outcome is known. A host
        creating a temporary scope must explicitly hand it to ScanAutomationOwner
        rather than relying on automatic detection or ordinary-run provisioning.
        """
        if type(automation) is not AndroidAutomationOwner:
            raise TypeError(
                "actual Android automation owner required for helper handoff"
            )
        if self._phase != "prepared" or self._failure is not None:
            raise RuntimeError(
                "Android helper preparation is not usable"
            ) from self._failure
        if phone is not self._phone or run_id != self._run_id:
            raise RuntimeError("helper handoff belongs to another phone/run")
        self._guard(deadline)
        automation.require_helper_scope(
            phone=phone,
            run_id=run_id,
            appium_home=self._home,
            expected_apk_sha256=self._expected,
            system_port=self._port,
            scenario_deadline=self._deadline,
            deadline=deadline,
        )
        self._guard(deadline)

    def restore(
        self, *, automation: AndroidAutomationOwner | None, deadline: float
    ) -> None:
        """Retire forever; restore only attempt-owned mutations, never the phone.

        After successful preparation, the actual automation owner's public guard
        must verify closure or that no session was submitted. None is only valid
        for a failed prepare, before a host could request its own session. Errors
        leave the lease retained. A verified rollback does not erase the separate
        preparation error or prove session/host/RF success.
        """
        if self._phase not in {"prepared", "prepare-failed"}:
            raise RuntimeError("Android helper restoration is one-shot after prepare")
        prepared = self._phase == "prepared"
        self._phase = "restoring"
        after: str | None = None
        try:
            self._guard(deadline)
            if automation is None:
                if prepared:
                    raise RuntimeError(
                        "actual automation owner required after preparation"
                    )
            else:
                if type(automation) is not AndroidAutomationOwner:
                    raise RuntimeError("actual Android automation owner required")
                automation.require_helper_scope(
                    phone=self._phone,
                    run_id=self._run_id,
                    appium_home=self._home,
                    expected_apk_sha256=self._expected,
                    system_port=self._port,
                    scenario_deadline=self._deadline,
                    deadline=deadline,
                )
                automation.require_helper_restoration(
                    phone=self._phone,
                    run_id=self._run_id,
                    appium_home=self._home,
                    expected_apk_sha256=self._expected,
                    system_port=self._port,
                    deadline=deadline,
                )
            self._inventory("restore-before", deadline)
            self._require_idle(deadline)
            errors: list[BaseException] = []
            if "store-create" in self._attempts:
                try:
                    raw = self._command(deadline, "shell", _READ_STORE)
                    if raw != "absent\n":
                        if self._pin is None or self._store_pin(raw) != self._pin:
                            raise RuntimeError(
                                "helper store ownership changed or unknown"
                            )
                        self._command(
                            deadline,
                            "shell",
                            f"[ ! -L {_STORE} ] && [ -f {_STORE} ] "
                            f"&& [ \"$({_STAT})\" = '{self._pin}' ] "
                            f"&& printf '%s\\n' '[\"io.appium.settings\"]' | cmp -s - {_STORE} "
                            f"&& rm {_STORE}",
                            mutation="store-delete",
                        )
                    if self._command(deadline, "shell", _READ_STORE) != "absent\n":
                        raise RuntimeError("helper store removal not confirmed")
                except BaseException as error:  # noqa: BLE001 -- still revoke attributable helper grant on store failure.
                    errors.append(error)
            if "appops-allow" in self._attempts:
                try:
                    self._inventory("restore-appops", deadline)
                    after = self._command(deadline, *_APPOPS)
                    if parse_mock_location_scopes(after) == {"package": "allow"}:
                        self._set_op(deadline, "deny")
                        after = self._command(deadline, *_APPOPS)
                    require_mock_location_restored(self._before, after)
                except BaseException as error:  # noqa: BLE001 -- retain cleanup causes separately from preparation.
                    errors.append(error)
            if self._unknown:
                errors.append(
                    RuntimeError(
                        f"unknown helper mutation completion: {sorted(self._unknown)}"
                    )
                )
            if self._uncertain_publications:
                errors.append(
                    RuntimeError(
                        f"unknown helper evidence publication: {sorted(self._uncertain_publications)}"
                    )
                )
            if errors:
                primary = errors[0]
                for error in errors[1:]:
                    primary.add_note(f"helper cleanup: {error}")
                raise primary
            if self._baseline is not None:
                final = self._inventory("restored-inventory", deadline)
                self._require_idle(deadline)
                if "appops-allow" in self._attempts:
                    after = self._command(deadline, *_APPOPS)
                    require_mock_location_restored(self._before, after)
                    if final[
                        "observed_mock_location_scopes"
                    ] != parse_mock_location_scopes(after):
                        raise RuntimeError("restored helper AppOps changed")
                if (
                    "store-create" in self._attempts
                    and self._command(deadline, "shell", _READ_STORE) != "absent\n"
                ):
                    raise RuntimeError("restored helper store reappeared")
            self._publish(
                "restored.json",
                {
                    "status": "restored",
                    "run_id": self._run_id,
                    "preparation_error": None
                    if self._failure is None
                    else str(self._failure),
                    "before": self._before,
                    "after": after,
                    "record_shape_changed": self._before is not None
                    and after is not None
                    and parse_mock_location_scopes(self._before)
                    != parse_mock_location_scopes(after),
                    "host_gate_status": "pending",
                    "phone_released": False,
                },
                deadline=deadline,
            )
            self._guard(deadline)
            self._phase = "restored"
        except BaseException as error:
            self._restore_failure, self._phase = error, "quarantined"
            self._report_failure("restore-failed.json", error)
            raise

    def require_restored(self) -> None:
        """Local evidence guard only; does not claim live session/resource zero."""
        if self._phase != "restored" or self._restore_failure is not None:
            raise RuntimeError(
                "Android helper restoration is not verified"
            ) from self._restore_failure
        try:
            self._records_current()
        except Exception as error:
            self._restore_failure = error
            raise
