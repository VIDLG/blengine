"""Owned, bounded Android radio control with raw OS readback evidence.

This is a host test tool, not an App/backend API. A successful svc command is
never evidence that the radio reached a state. Ordinary callers must explicitly
select a device and retain this controller until their App/fixture work ends.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tools.android import adb

ROOT = Path(__file__).resolve().parents[2]
STATE = re.compile(
    r"^\s*state:\s*(ON|OFF|TURNING_ON|TURNING_OFF|BLE_ON|BLE_TURNING_ON|BLE_TURNING_OFF)\s*$",
    re.MULTILINE,
)
ACTION_SECONDS = 20.0
COMMAND_SECONDS = 5.0
POLL_SECONDS = 0.1
MAX_OUTPUT_BYTES = 512 * 1024


CONTROL_STATES = (
    "ON",
    "OFF",
    "TURNING_ON",
    "TURNING_OFF",
    "BLE_ON",
    "BLE_TURNING_ON",
    "BLE_TURNING_OFF",
)
CONTROL_STATUSES = ("running", "passed", "failed", "quarantined")
CONTROL_KEYS = {
    "schema_version",
    "platform",
    "device",
    "run_id",
    "status",
    "initial_state",
    "final_state",
    "actions",
    "primary_error",
    "cleanup_errors",
}
ACTION_KEYS = {
    "label",
    "requested_state",
    "observed_state",
    "started_monotonic",
    "confirmed_monotonic",
    "receipts",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def verify_state_control(document: Any, run_id: str, device: str) -> None:
    """Validate a published radio-control journal against its own contract.

    This checks the envelope this module writes: identity binding, status/state
    vocabulary, action order and receipt shape. It is an unsigned host record,
    not device attestation, exclusive-radio proof or an RF verdict.
    """
    _require(type(document) is dict and set(document) == CONTROL_KEYS, "control schema")
    _require(
        document["schema_version"] == 1
        and type(document["schema_version"]) is int
        and document["platform"] == "android",
        "control schema version or platform",
    )
    _require(
        document["device"] == device and document["run_id"] == run_id,
        "control identity mismatch",
    )
    _require(document["status"] in CONTROL_STATUSES, "control status")
    _require(
        document["initial_state"] is None or document["initial_state"] == "ON",
        "control initial state must be ON or unset",
    )
    _require(
        document["final_state"] is None or document["final_state"] in CONTROL_STATES,
        "control final state",
    )
    _require(
        document["primary_error"] is None or type(document["primary_error"]) is str,
        "control primary error",
    )
    _require(
        type(document["cleanup_errors"]) is list
        and all(type(item) is str for item in document["cleanup_errors"]),
        "control cleanup errors",
    )
    actions = document["actions"]
    _require(type(actions) is list, "control actions")
    previous = 0.0
    for action in actions:
        _require(
            type(action) is dict and set(action) == ACTION_KEYS, "control action schema"
        )
        _require(
            type(action["label"]) is str and action["label"] != "",
            "control action label",
        )
        _require(action["requested_state"] in CONTROL_STATES, "control requested state")
        _require(
            action["observed_state"] is None
            or action["observed_state"] in CONTROL_STATES,
            "control observed state",
        )
        for key in ("started_monotonic", "confirmed_monotonic"):
            value = action[key]
            _require(
                value is None or (_finite(value) and value >= previous),
                f"control action {key}",
            )
            if value is not None:
                previous = value
        receipts = action["receipts"]
        _require(type(receipts) is list, "control receipts")
        for receipt in receipts:
            _require(type(receipt) is dict, "control receipt")
            _require(
                type(receipt.get("command")) is list
                and all(type(part) is str for part in receipt["command"])
                and _finite(receipt.get("started_monotonic")),
                "control receipt command/timing",
            )
            if "error" in receipt:
                _require(type(receipt["error"]) is str, "control receipt error")
                continue
            _require(
                type(receipt.get("returncode")) is int
                and type(receipt.get("stdout")) is str
                and _finite(receipt.get("finished_monotonic")),
                "control receipt result",
            )


def radio_state(output: str) -> str:
    """Require one unambiguous BluetoothManager state, not a historical log entry."""
    values = STATE.findall(output)
    if len(values) != 1:
        raise RuntimeError("BluetoothManager must report exactly one radio state")
    return values[0]


class AndroidRadioController:
    """Own one explicit phone's radio changes and restore its required initial ON.

    The directory lock coordinates cooperating instances of this controller;
    it is not an OS-wide Bluetooth lock. Never adopt or remove a pre-existing
    lock, even if its recorded PID appears dead. The journal is unsigned host
    evidence, not device attestation or proof of exclusive system radio use.
    """

    def __init__(
        self,
        device: str,
        run_id: str,
        evidence_path: Path,
        *,
        lock_root: Path | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", device):
            raise ValueError("an explicit canonical Android serial is required")
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("run_id must be a canonical UUID")
        self.device, self.run_id, self.path = device, run_id, evidence_path
        self.lock_root = lock_root or ROOT / ".work/android-radio-leases"
        self.lock = self.lock_root / hashlib.sha256(device.encode()).hexdigest()
        self.owner: bytes | None = None
        self.lock_identity: tuple[int, int] | None = None
        # Identity of the owner file this controller created; see _require_owner.
        self._owner_signature: tuple[int, ...] | None = None
        self.initial_state: str | None = None
        self.last_state: str | None = None
        self.finished = False
        self._app_observation_finishing = False
        self.document: dict[str, Any] = {
            "schema_version": 1,
            "platform": "android",
            "device": device,
            "run_id": run_id,
            "status": "running",
            "initial_state": None,
            "final_state": None,
            "actions": [],
            "primary_error": None,
            "cleanup_errors": [],
        }

    def _publish(self) -> None:
        import json

        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(self.document, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def acquire(self) -> None:
        """Create a phone-specific lock, refusing running and stale owners alike."""
        import json

        if self.owner is not None or self.finished:
            raise RuntimeError("radio controller cannot be reacquired")
        if self.path.exists() or self.path.is_symlink():
            raise RuntimeError("refusing existing radio-control evidence")
        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.lock_root.is_symlink():
            raise RuntimeError("radio lock root must not be a symlink")
        self.lock.mkdir(mode=0o700)
        info = self.lock.lstat()
        self.lock_identity = (info.st_dev, info.st_ino)
        self.owner = json.dumps(
            {"device": self.device, "run_id": self.run_id, "host_pid": os.getpid()},
            sort_keys=True,
        ).encode()
        try:
            owner_path = self.lock / "owner.json"
            with owner_path.open("xb") as stream:
                stream.write(self.owner)
            info = owner_path.lstat()
            self._owner_signature = (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            self._publish()
        except BaseException:
            self._release()
            raise

    def _require_owner(self) -> None:
        if self.owner is None or self.finished:
            raise RuntimeError("radio controller does not own the phone lock")
        info = self.lock.lstat()
        owner_path = self.lock / "owner.json"
        owner_info = owner_path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != self.lock_identity
            or not stat.S_ISREG(owner_info.st_mode)
            or owner_info.st_nlink != 1
            or owner_info.st_size != len(self.owner)
        ):
            raise RuntimeError("radio controller ownership changed; refusing mutation")
        # A lease guard runs at every mutation and polling boundary, so reading the owner
        # file each time dominates a long UI run. The file's identity answers the common
        # case without IO; identical bytes after any change still keep the lease.
        signature = (
            owner_info.st_dev,
            owner_info.st_ino,
            owner_info.st_mode,
            owner_info.st_nlink,
            owner_info.st_size,
            owner_info.st_mtime_ns,
            owner_info.st_ctime_ns,
        )
        if signature != self._owner_signature:
            # Identity is not authoritative: the owner bytes are. Re-read only here, so a
            # restore of the exact bytes keeps the lease (as before) while the common
            # guard path costs one lstat.
            if owner_path.read_bytes() != self.owner:
                raise RuntimeError(
                    "radio controller ownership changed; refusing mutation"
                )
            self._owner_signature = signature

    def require_owner(self) -> None:
        """Check the phone lease before App mutations as well as radio commands."""
        self._require_owner()

    def _command(self, action: dict[str, Any], deadline: float, *args: str) -> str:
        self._require_owner()
        remaining = deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("Android radio action deadline exceeded")
        command = adb.adb_command(self.device, "shell", *args)
        receipt: dict[str, Any] = {
            "command": command,
            "started_monotonic": time.monotonic(),
        }
        action["receipts"].append(receipt)
        try:
            # Tool-path lookup and receipt creation also consume the action budget.
            remaining = deadline - time.monotonic()
            if not math.isfinite(remaining) or remaining <= 0:
                raise TimeoutError(
                    "Android radio action deadline exceeded before command"
                )
            result = adb.run_command(
                command,
                cwd=ROOT,
                timeout=min(COMMAND_SECONDS, remaining),
                check=False,
            )
            receipt.update(returncode=result.returncode, stdout=result.stdout)
            if len(result.stdout.encode()) > MAX_OUTPUT_BYTES:
                # Preserve bounded evidence rather than an arbitrarily large dump.
                receipt["stdout"] = result.stdout[:MAX_OUTPUT_BYTES]
                raise RuntimeError("Android state command output exceeded limit")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Android radio command returned after the action deadline"
                )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Android state command failed: {command}: {result.stdout[-1000:]}"
                )
            return result.stdout
        except (OSError, subprocess.SubprocessError) as error:
            receipt.update(error=str(error))
            raise
        finally:
            receipt["finished_monotonic"] = time.monotonic()
            self._publish()

    def _action(
        self, label: str, target: str, *, change: bool, timeout: float = ACTION_SECONDS
    ) -> None:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("radio action timeout must be positive and finite")
        started = time.monotonic()
        self._require_owner()
        action: dict[str, Any] = {
            "label": label,
            "requested_state": target,
            "observed_state": None,
            "started_monotonic": started,
            "confirmed_monotonic": None,
            "receipts": [],
        }
        self.document["actions"].append(action)
        deadline = started + timeout
        try:
            if change:
                self._command(
                    action,
                    deadline,
                    "svc",
                    "bluetooth",
                    "enable" if target == "ON" else "disable",
                )
            while True:
                self.last_state = radio_state(
                    self._command(action, deadline, "dumpsys", "bluetooth_manager")
                )
                action["observed_state"] = self.last_state
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Android radio action deadline exceeded after readback"
                    )
                if self.last_state == target:
                    action["confirmed_monotonic"] = time.monotonic()
                    return
                if not change:
                    raise RuntimeError(
                        f"Android radio precondition {target}, observed {self.last_state}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Android radio did not reach {target}")
                time.sleep(min(POLL_SECONDS, remaining))
        finally:
            self._publish()

    def initial(self) -> None:
        """Require ON without changing a pre-existing OFF/transitioning phone."""
        if self.document["actions"]:
            raise RuntimeError("initial radio observation must be first")
        self._action("initial", "ON", change=False)
        self.initial_state = "ON"
        self.document["initial_state"] = "ON"
        self._publish()

    def off(self, *, timeout: float = ACTION_SECONDS) -> None:
        """Disable once within timeout seconds, including command and polling time.

        Each subprocess gets at most the remaining action budget. This per-call
        timeout never changes the independent default budgets used by finish().
        """
        if [a["label"] for a in self.document["actions"]] != ["initial"]:
            raise RuntimeError("OFF must follow initial ON")
        self._action("off", "OFF", change=True, timeout=timeout)

    def on(self) -> None:
        """Enable once after confirmed OFF; no retries of the mutation command."""
        if [a["label"] for a in self.document["actions"]] != [
            "initial",
            "off",
        ] or self.last_state != "OFF":
            raise RuntimeError("ON must follow confirmed OFF")
        self._action("on", "ON", change=True)

    def _release(self) -> None:
        self._require_owner()
        (self.lock / "owner.json").unlink()
        self.lock.rmdir()
        self.owner = None
        self.lock_identity = None

    def finish_app_observation(
        self,
        primary_error: BaseException | None,
        *,
        finalize_owned: Callable[[], None],
    ) -> list[str]:
        """Finalize an App-only lease without reading or changing the radio.

        Publish before release; any cleanup uncertainty quarantines the phone.
        The journal and lock are not a crash-atomic transaction. If quarantine
        publication also fails, retract only our provisional terminal journal.
        """
        import json

        if self.finished or self._app_observation_finishing:
            return ["radio controller was already finished or is finishing"]
        if self.owner is None:
            return ["radio controller was not acquired"]
        self._app_observation_finishing = True
        errors: list[str] = []
        provisional: bytes | None = None
        self.document.update(
            primary_error=None
            if primary_error is None
            else f"{type(primary_error).__name__}: {primary_error}",
            cleanup_errors=errors,
        )
        try:
            self.require_owner()
            self._require_app_observation()
            finalize_owned()
            self.require_owner()
            self._require_app_observation()
            self.document["status"] = "passed" if primary_error is None else "failed"
            provisional = (
                json.dumps(self.document, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            self._publish()
            self._release_app_observation()
        except BaseException as error:  # noqa: BLE001 - interruption must keep the lock
            errors.append(
                "App observation finalization: "
                + "".join(format_exception_only(error)).strip()
            )
            self.document["status"] = "quarantined"
            try:
                self._publish()
            except BaseException as publication:  # noqa: BLE001 - preserve cleanup errors
                errors.append(
                    "App observation quarantine publication: "
                    + "".join(format_exception_only(publication)).strip()
                )
                if provisional is not None:
                    try:
                        info = self.path.lstat()
                        if (
                            stat.S_ISREG(info.st_mode)
                            and info.st_nlink == 1
                            and self.path.read_bytes() == provisional
                        ):
                            self.path.unlink()
                    except BaseException as retraction:  # noqa: BLE001 - retain all failures
                        errors.append(
                            "App observation journal retraction: "
                            + "".join(format_exception_only(retraction)).strip()
                        )
        finally:
            self.finished = True
        return errors

    def _require_app_observation(self) -> None:
        if (
            self.document["actions"]
            or self.initial_state is not None
            or self.last_state is not None
        ):
            raise RuntimeError("App observation requires no radio actions or state")

    def _release_app_observation(self) -> None:
        """Compensate partial deletion only for the new App-only finish path."""
        try:
            self._release()
        except BaseException as error:
            try:
                # _release may have unlinked owner.json before rmdir failed.
                # Never recreate a directory or overwrite an unknown owner.
                info = self.lock.lstat()
                if (
                    self.owner is None
                    or not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != self.lock_identity
                ):
                    raise RuntimeError("phone lock changed during release")
                owner_path = self.lock / "owner.json"
                if not owner_path.exists() and not owner_path.is_symlink():
                    with owner_path.open("xb") as stream:
                        stream.write(self.owner)
                self.require_owner()
            except BaseException as restoration:  # noqa: BLE001 - keep release and recovery errors
                error.add_note(
                    "App observation owner retention: "
                    + "".join(format_exception_only(restoration)).strip()
                )
            raise

    def finish_observation(
        self,
        primary_error: BaseException | None,
        *,
        on_failure: Callable[[], None] | None = None,
    ) -> list[str]:
        """Final-read ON and release without changing radio state, even on failure.

        Permission gates restore their grants before calling this method. If the
        final read fails, the caller can stop its owned App before lease release.
        Unlike finish(), this path never requires OFF/ON or attempts svc recovery.
        """
        errors: list[str] = []
        if self.finished:
            return ["radio controller was already finished"]
        self.document["primary_error"] = (
            str(primary_error) if primary_error is not None else None
        )
        try:
            self.require_owner()
            self._action("final", "ON", change=False)
            self.document["final_state"] = "ON"
            if self.initial_state != "ON":
                errors.append("initial radio ON precondition not established")
        except Exception as error:  # noqa: BLE001 - finalize under every readback failure
            errors.append(f"final radio ON readback: {error}")
        finally:
            if errors and on_failure is not None:
                try:
                    on_failure()
                except Exception as error:  # noqa: BLE001 - preserve final ON failure
                    errors.append(f"owned app cleanup after final ON failure: {error}")
            try:
                self._release()
            except Exception as error:  # noqa: BLE001 - preserve the primary cause
                errors.append(f"phone owner release: {error}")
            self.finished = True
            self.document.update(
                status="failed" if primary_error is not None or errors else "passed",
                cleanup_errors=errors,
            )
            try:
                self._publish()
            except Exception as error:  # noqa: BLE001 - preserve cleanup failures
                errors.append(f"radio journal finalization: {error}")
        return errors

    def finish(self, primary_error: BaseException | None) -> list[str]:
        """Restore ON on failure, preserve primary/cleanup errors and release only our lock.

        Successful evidence has exactly initial/off/on/final. Recovery actions
        remain failed-attempt evidence and cannot turn a partial run into passed.
        """
        errors: list[str] = []
        if self.finished:
            return ["radio controller was already finished"]
        self.document["primary_error"] = (
            str(primary_error) if primary_error is not None else None
        )
        try:
            self._require_owner()
            labels = [a["label"] for a in self.document["actions"]]
            if self.initial_state == "ON":
                if primary_error is not None or labels != ["initial", "off", "on"]:
                    try:
                        self._action("restore", "ON", change=True)
                    except (
                        OSError,
                        ValueError,
                        RuntimeError,
                        subprocess.SubprocessError,
                    ) as error:
                        errors.append(f"restore radio: {error}")
                try:
                    self._action("final", "ON", change=False)
                    self.document["final_state"] = "ON"
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    subprocess.SubprocessError,
                ) as error:
                    errors.append(f"final radio readback: {error}")
                    # A newly observed final failure must also restore the phone.
                    # Keep the error even if bounded recovery succeeds.
                    try:
                        self._action("restore", "ON", change=True)
                        self._action("final", "ON", change=False)
                        self.document["final_state"] = "ON"
                    except (
                        OSError,
                        ValueError,
                        RuntimeError,
                        subprocess.SubprocessError,
                    ) as recovery_error:
                        errors.append(f"final radio recovery: {recovery_error}")
                if primary_error is None and labels != ["initial", "off", "on"]:
                    errors.append("radio transition did not complete initial/off/on")
            elif primary_error is None:
                errors.append("initial ON precondition was not established")
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(f"radio ownership: {error}")
        finally:
            try:
                self._release()
            except (OSError, RuntimeError) as error:
                errors.append(f"radio lock release: {error}")
            self.finished = True
            self.document["cleanup_errors"] = errors
            self.document["status"] = (
                "passed" if primary_error is None and not errors else "failed"
            )
            self._publish()
        return errors
