"""Named iOS radio/observation phone owner; no radio, App or unlock operations.

Acquire in the fixture's host process before installation, fixture acquisition or
App work, and require_owner before each such action. Like AndroidRadioController,
this coordinates cooperating hosts, not system-wide radio access. Never adopt a
stale owner. Radio callers retain their primary error and hash the exact persisted
UI control bytes (not a reserialization) before passing that document to finish.
Observation callers use finish_observation with their host-owned finalizer instead.
A released lease is cleanup evidence, not an RF or bundle verdict.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tools.react_native.evidence_io import (
    open_regular as _open_regular,
)
from tools.react_native.evidence_io import (
    read_small as _read_small,
)
from tools.react_native.evidence_io import (
    recovery_json as _recovery_json,
)
from tools.react_native.host_paths import PROJECT_ROOT as ROOT
from tools.react_native.ios_device import canonical_physical_udid
from tools.react_native.ios_radio_control import (
    CONTROL_STEPS as _CONTROL_STEPS,
)
from tools.react_native.ios_radio_control import (
    identity as _identity,
)
from tools.react_native.ios_radio_control import (
    verify_control,
)
from tools.react_native.ios_radio_restoration import (
    INSPECTION_MAX_AGE_SECONDS,
    collect_inspection,
    verify_inspection,
)

SCOPE = "ios-adapter-startup-phone-owner"
NATIVE_SYSTEM_LOSS_SCOPE = "ios-bleng-system-loss-phone-owner"
SCAN_OBSERVATION_SCOPE = "ios-formal-scan-phone-owner"
NATIVE_MULTI_LINK_SCOPE = "ios-bleng-multi-link-phone-owner"
_NATIVE_CONTROL_SCOPE = "ios-bleng-system-loss-control"
# Scopes that mutate no radio state, so an observation-only finish may release them. A scope
# that changes the adapter, permissions or Settings is never releasable this way.
OBSERVATION_SCOPES = frozenset({SCAN_OBSERVATION_SCOPE, NATIVE_MULTI_LINK_SCOPE})
_CONTROL_KEYS = {
    "schema_version",
    "scope",
    "run_id",
    "fixture_receipt_sha256",
    "status",
    "mutation_attempted",
    "restored",
    "same_session",
    "observations",
    "primary_error",
    "cleanup_error",
}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory(path: Path, *, create: bool = False) -> int:
    """Walk without following symlinks, including intermediate components."""
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _hash(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class ExistingPhoneLeaseError(FileExistsError):
    """Another lease holds this device; the name cannot be acquired.

    A `FileExistsError` because that is the raw condition and the historic contract,
    and a named class so a caller can react to *this* refusal without parsing text.
    """


class IosRadioLease:
    """One physical UDID, run and in-process fixture PID; quarantine is terminal.

    Modern physical UDIDs normalize to uppercase, legacy 40-hex UDIDs to
    lowercase, so case aliases cannot acquire independent phone locks. There is
    deliberately no stale adoption, context-manager auto-release or clear API.
    """

    def __init__(
        self,
        device: str,
        run_id: str,
        evidence_path: Path,
        *,
        lock_root: Path | None = None,
        scope: str = SCOPE,
    ) -> None:
        _require(
            scope
            in {
                SCOPE,
                NATIVE_SYSTEM_LOSS_SCOPE,
                SCAN_OBSERVATION_SCOPE,
                NATIVE_MULTI_LINK_SCOPE,
            },
            "unsupported iOS phone lease scope",
        )
        device = canonical_physical_udid(device)
        if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
            raise ValueError("run_id must be a canonical UUID")
        self.device, self.run_id, self.scope = device, run_id, scope
        self.host_pid = os.getpid()
        self.path = Path(evidence_path).absolute()
        self.lock_root = Path(lock_root or ROOT / ".work/ios-radio-leases").absolute()
        self.lock = self.lock_root / hashlib.sha256(device.encode()).hexdigest()
        # Do not allow a journal to replace owner.json or become a lease child.
        if self.path == self.lock_root or self.lock_root in self.path.parents:
            raise ValueError("evidence must be outside the phone lock root")
        self.owner: bytes | None = None
        self.lock_identity: tuple[int, int] | None = None
        self._root_identity: tuple[int, int] | None = None
        self._owner_identity: tuple[int, int] | None = None
        self._journal_identity: tuple[int, int] | None = None
        self._journal_raw: bytes | None = None
        self._published_status: str | None = None
        self._started = False
        self.finished = False
        self.document: dict[str, Any] = {
            "schema_version": 1,
            "platform": "ios",
            "device": device,
            "run_id": run_id,
            "host_pid": self.host_pid,
            "status": "held",
            "scope": self.scope,
            "control_sha256": None,
            "restoration": "unverified",
            "error": None,
            "acquired_monotonic": 0.0,
            "released_monotonic": None,
        }
        if self.scope == SCAN_OBSERVATION_SCOPE:
            self.document.update(primary_error=None, cleanup_errors=[])

    def _owned_directory(self) -> int:
        _require(
            self.owner is not None and os.getpid() == self.host_pid,
            "iOS phone lease is not owned by this process",
        )
        root = _directory(self.lock_root)
        try:
            info = os.fstat(root)
            _require(
                _inode(info) == self._root_identity and info.st_uid == os.getuid(),
                "iOS phone lock root ownership changed",
            )
            lock = os.open(self.lock.name, _DIRECTORY_FLAGS, dir_fd=root)
        finally:
            os.close(root)
        try:
            info = os.fstat(lock)
            _require(
                _inode(info) == self.lock_identity and info.st_uid == os.getuid(),
                "iOS phone lock directory ownership changed",
            )
            self._check_file(lock, "owner.json", self._owner_identity, self.owner)
            return lock
        except BaseException:
            os.close(lock)
            raise

    @staticmethod
    def _check_file(
        directory: int, name: str, identity: tuple[int, int] | None, raw: bytes | None
    ) -> None:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and _inode(info) == identity
            and raw is not None
            and info.st_size == len(raw),
            f"iOS lease {name} inode/type/link/owner changed",
        )
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            _require(
                _inode(opened) == identity
                and stat.S_ISREG(opened.st_mode)
                and opened.st_uid == os.getuid()
                and opened.st_nlink == 1
                and opened.st_size == len(raw)
                and stream.read(len(raw) + 1) == raw,
                f"iOS lease {name} raw owner/content changed",
            )

    def require_owner(self) -> None:
        """Guard every installation, fixture and App action in this host process."""
        _require(not self.finished, "iOS phone lease is already finished")
        descriptor = self._owned_directory()
        os.close(descriptor)

    def _write_owner(self, directory: int) -> None:
        descriptor = os.open(
            "owner.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            self._owner_identity = _inode(os.fstat(stream.fileno()))
            stream.write(self.owner)
            stream.flush()
            os.fsync(stream.fileno())

    def acquire(self) -> None:
        """Acquire before any phone/fixture work; existing owners are never touched."""
        _require(not self._started, "iOS phone lease cannot be reacquired")
        _require(os.getpid() == self.host_pid, "iOS phone lease host PID changed")
        self._started = True
        parent = _directory(self.path.parent)
        try:
            try:
                os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("refusing existing iOS phone lease evidence")
        finally:
            os.close(parent)
        root = _directory(self.lock_root, create=True)
        try:
            fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _require(
                not os.path.lexists(
                    self.lock_root / (self.lock.name + ".restoration-pending")
                ),
                "pending restoration blocks phone acquisition",
            )
            info = os.fstat(root)
            _require(info.st_uid == os.getuid(), "foreign iOS phone lock root owner")
            self._root_identity = _inode(info)
            try:
                os.mkdir(self.lock.name, mode=0o700, dir_fd=root)
            except FileExistsError:
                # Not a race to retry and never adopted: an existing owner (usually a
                # quarantine, which is terminal) holds this device. Say which lease it is,
                # because a bare Errno 17 with a hash for a name is unactionable.
                raise ExistingPhoneLeaseError(
                    f"device {self.device} is held by an existing iOS phone lease"
                    + _owner_hint(root, self.lock.name)
                ) from None
            directory = os.open(self.lock.name, _DIRECTORY_FLAGS, dir_fd=root)
        finally:
            os.close(root)
        try:
            self.lock_identity = _inode(os.fstat(directory))
            self.owner = json.dumps(
                {
                    "device": self.device,
                    "run_id": self.run_id,
                    "host_pid": self.host_pid,
                    "scope": self.scope,
                    "owner_nonce": uuid.uuid4().hex,
                },
                sort_keys=True,
            ).encode()
            self.document["acquired_monotonic"] = time.monotonic()
            self._write_owner(directory)
        finally:
            os.close(directory)
        try:
            self._publish()
        except BaseException as error:
            # No caller work is permitted until acquire returns. Still prove the
            # raw owner/inodes: a publication failure never licenses foreign cleanup.
            try:
                self._release()
            except Exception as cleanup:  # noqa: BLE001 - retain acquisition failure
                error.add_note(f"iOS phone acquisition cleanup: {cleanup}")
            self.finished = True
            raise

    def _publication_guard(self) -> None:
        if self.document["status"] == "quarantined":
            # Ownership loss must still be recorded in our own journal, without
            # touching the now-foreign phone lock.
            _require(os.getpid() == self.host_pid, "iOS phone lease host PID changed")
        else:
            self.require_owner()

    def _publish(self) -> None:
        self._publication_guard()
        raw = (json.dumps(self.document, sort_keys=True) + "\n").encode()
        parent = _directory(self.path.parent)
        temporary = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        created = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                identity = _inode(os.fstat(stream.fileno()))
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._publication_guard()
            if self._journal_identity is None:
                # Unlike replace, link cannot overwrite an evidence-path racer.
                os.link(
                    temporary,
                    self.path.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=parent)
                created = False
            else:
                self._check_file(
                    parent, self.path.name, self._journal_identity, self._journal_raw
                )
                os.replace(
                    temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent
                )
                created = False
            self._journal_identity, self._journal_raw = identity, raw
            self._published_status = self.document["status"]
        finally:
            if created:
                os.unlink(temporary, dir_fd=parent)
            os.close(parent)

    def _release(self) -> None:
        directory = self._owned_directory()
        try:
            _require(
                os.listdir(directory) == ["owner.json"], "foreign phone lock children"
            )
            root = _directory(self.lock_root)
            try:
                _require(
                    _inode(os.fstat(root)) == self._root_identity,
                    "phone lock root drift",
                )
                self.require_owner()
                os.unlink("owner.json", dir_fd=directory)
                try:
                    info = os.stat(self.lock.name, dir_fd=root, follow_symlinks=False)
                    _require(
                        _inode(info) == self.lock_identity, "phone lock directory drift"
                    )
                    os.rmdir(self.lock.name, dir_fd=root)
                except BaseException:
                    # Do not leave an ownerless lock on a failed rmdir. Exclusive
                    # creation refuses any foreign replacement during rollback.
                    self._write_owner(directory)
                    raise
                self.owner = None
            finally:
                os.close(root)
        finally:
            os.close(directory)

    def _native_restoration(self, control: dict | None) -> str:
        _require(type(control) is dict, "native loss control is required")
        expected = {
            "schema_version",
            "scope",
            "run_id",
            "fixture_receipt_sha256",
            "status",
            "mutation_attempted",
            "restored",
            "same_session",
            "observations",
            "primary_error",
            "cleanup_error",
        }
        _require(set(control) == expected, "native loss control exact schema")
        _require(
            control["schema_version"] == 1
            and control["scope"] == _NATIVE_CONTROL_SCOPE
            and control["run_id"] == self.run_id
            and _hash(control["fixture_receipt_sha256"]),
            "native loss control identity/hash",
        )
        _require(
            type(control["mutation_attempted"]) is bool
            and type(control["restored"]) is bool
            and type(control["same_session"]) is bool
            and type(control["primary_error"]) is str
            and type(control["cleanup_error"]) is str
            and control["status"] in ("passed", "failed"),
            "native loss control flags/status",
        )
        observations = control["observations"]
        _require(
            type(observations) is list and len(observations) <= 16,
            "native loss observations",
        )
        if not observations:
            _require(
                not control["mutation_attempted"]
                and not control["restored"]
                and not control["same_session"]
                and control["status"] == "failed",
                "empty native loss observations require pre-mutation failure",
            )
            return "not-mutated"
        previous = -1.0
        for row in observations:
            _require(
                type(row) is dict
                and set(row)
                == {"action", "elapsed_seconds", "identity", "switch_value"},
                "native loss observation schema",
            )
            elapsed = row["elapsed_seconds"]
            _require(
                type(elapsed) in (int, float)
                and math.isfinite(elapsed)
                and elapsed >= previous,
                "native loss observation timing",
            )
            previous = float(elapsed)
            _require(row["switch_value"] in ("0", "1"), "native loss switch readback")
        last = observations[-1]["switch_value"]
        if control["status"] == "passed":
            _require(
                control["mutation_attempted"]
                and control["restored"]
                and control["same_session"]
                and not control["primary_error"]
                and not control["cleanup_error"]
                and last == "1",
                "native loss pass requires same-owner ON restoration",
            )
            return "on-readback"
        if control["mutation_attempted"]:
            _require(
                control["restored"] and not control["cleanup_error"] and last == "1",
                "native loss failure requires verified ON cleanup",
            )
            return "on-readback"
        _require(
            not control["restored"] and not control["same_session"],
            "native loss no-mutation flags",
        )
        return "not-mutated"

    def _restoration(self, control: dict | None, control_sha256: str | None) -> str:
        _require(_hash(control_sha256), "missing or malformed source control SHA-256")
        if self.scope == NATIVE_SYSTEM_LOSS_SCOPE:
            return self._native_restoration(control)
        _require(
            type(control) is dict and set(control) == _CONTROL_KEYS,
            "control exact schema",
        )
        _require(
            type(control["schema_version"]) is int
            and control["schema_version"] == 1
            and control["scope"] == "ios-adapter-startup-control-window"
            and control["run_id"] == self.run_id
            and _hash(control["fixture_receipt_sha256"]),
            "foreign/malformed control version, scope, run or fixture hash",
        )
        _require(
            all(
                type(control[key]) is bool
                for key in ("mutation_attempted", "restored", "same_session")
            )
            and type(control["primary_error"]) is str
            and type(control["cleanup_error"]) is str
            and control["status"] in ("passed", "failed"),
            "control boolean flags, status or errors",
        )
        observations = control["observations"]
        _require(
            type(observations) is list and len(observations) <= 13,
            "control observation bound",
        )
        cleanup = (
            bool(
                observations and observations[-1].get("action") == "cleanup-restored-on"
            )
            if all(type(item) is dict for item in observations)
            else False
        )
        steps = list(_CONTROL_STEPS)
        if cleanup:
            _require(control["status"] == "failed", "cleanup is not successful control")
            steps = steps[: len(observations) - 1] + [
                ("cleanup-restored-on", False, "1")
            ]
        _require(len(observations) <= len(steps), "control observation count")
        previous = 0.0
        identity = None
        for observation, (action, identified, switch) in zip(observations, steps):
            _require(
                type(observation) is dict
                and set(observation)
                == {"action", "elapsed_seconds", "identity", "switch_value"},
                "control observation exact schema",
            )
            _require(
                observation["action"] == action
                and observation["switch_value"] == switch,
                "control action order or real switch readback",
            )
            elapsed = observation["elapsed_seconds"]
            _require(
                type(elapsed) in (int, float)
                and math.isfinite(elapsed)
                and elapsed >= previous,
                "control finite nondecreasing elapsed",
            )
            previous = elapsed
            if identified:
                observed = _identity(observation["identity"], self.run_id)
                _require(
                    identity is None or observed == identity, "control identity drift"
                )
                identity = observed
            else:
                _require(observation["identity"] is None, "unexpected control identity")
        if control["status"] == "passed":
            _require(identity is not None, "missing successful control identity")
            verify_control(
                control,
                runtime={
                    "run_id": self.run_id,
                    "native_bootstrap": identity["native"],
                    "runtime_nonce": identity["nonce"],
                },
                receipt_sha256=control["fixture_receipt_sha256"],
            )
            return "on-readback"
        _require(
            bool(control["primary_error"]) and not control["same_session"],
            "failed control source flags/error",
        )
        _require(not control["cleanup_error"], "control cleanup failed")
        if not control["mutation_attempted"]:
            _require(
                not control["restored"] and len(observations) <= 1 and not cleanup,
                "no-mutation claim contains Settings/OFF actions or restoration",
            )
            return "not-mutated"
        _require(
            control["restored"] and cleanup and len(observations) >= 3,
            "missing initial ON or final cleanup ON readback",
        )
        return "on-readback"

    def finish(self, control: dict | None, control_sha256: str | None) -> list[str]:
        """Return cleanup errors only; uncertain restoration keeps the phone occupied.

        Publication precedes deletion so a publication failure keeps the lock.
        If deletion fails, retract the provisional released journal in favor of
        quarantine. This is not a crash-atomic transaction across two filesystems;
        consumers must also audit control, fixture PID and the final bundle.
        """
        if self.scope == SCAN_OBSERVATION_SCOPE:
            return ["observation scope requires finish_observation, not radio finish"]
        if self.finished:
            return ["iOS phone lease was already finished"]
        if self.owner is None:
            return ["iOS phone lease was not acquired"]
        errors: list[str] = []
        if _hash(control_sha256):
            self.document["control_sha256"] = control_sha256
        try:
            self.require_owner()
            self.document["restoration"] = self._restoration(control, control_sha256)
            self.document.update(status="released", released_monotonic=time.monotonic())
            self._publish()
            self._release()
        except Exception as error:  # noqa: BLE001 - caller retains its primary failure
            errors = self._quarantine(f"iOS phone lease finalization: {error}")
        self.finished = True
        return errors

    def finish_observation(
        self,
        primary_error: BaseException | None,
        *,
        finalize_owned: Callable[[], None],
    ) -> list[str]:
        """Return cleanup errors only, retaining the primary failure in this journal.

        This trusted host boundary requires a finalizer for the host's existing
        owned-App cleanup or successful-page retention checks. The host must bound
        its deadline. Callback success is not native resource-zero, radio readback,
        restoration or an RF verdict; this method performs no radio operations.
        """
        if self.scope not in OBSERVATION_SCOPES:
            return ["radio scope requires finish with restoration proof"]
        if self.finished:
            return ["iOS phone lease was already finished"]
        if self.owner is None:
            return ["iOS phone lease was not acquired"]
        self.document["primary_error"] = (
            None
            if primary_error is None
            else f"{type(primary_error).__name__}: {primary_error}"
        )
        errors: list[str] = []
        try:
            self.require_owner()
            finalize_owned()
            self.require_owner()
            self.document.update(status="released", released_monotonic=time.monotonic())
            self._publish()
            self._release()
        except BaseException as error:  # noqa: BLE001 - interruption must keep the lock
            errors = self._quarantine(
                "iOS phone observation finalization: "
                + "".join(format_exception_only(error)).strip()
            )
        self.finished = True
        return errors

    def _quarantine(self, message: str) -> list[str]:
        errors = [message]
        self.document.update(
            status="quarantined", released_monotonic=None, error=message
        )
        if self.scope in OBSERVATION_SCOPES:
            self.document["cleanup_errors"] = errors
        try:
            self._publish()
        except BaseException as publication:  # noqa: BLE001 - retain the primary failure
            errors.append(f"iOS phone quarantine publication: {publication}")
            # Never leave our provisional released claim after failed deletion.
            if self._published_status == "released":
                try:
                    parent = _directory(self.path.parent)
                    try:
                        self._check_file(
                            parent,
                            self.path.name,
                            self._journal_identity,
                            self._journal_raw,
                        )
                        os.unlink(self.path.name, dir_fd=parent)
                    finally:
                        os.close(parent)
                except BaseException as retraction:  # noqa: BLE001 - preserve cleanup errors
                    errors.append(
                        f"iOS phone released journal retraction: {retraction}"
                    )
        self.document["error"] = "; ".join(errors)
        return errors


def archive_phone_owner(
    device: str,
    run_id: str,
    *,
    original_status: str,
    journal: Path,
    inspection_input: Path | None,
    output: Path,
    owner_sha256: str,
    journal_sha256: str,
    authorize_restoration_only: bool = False,
    orphaned_capture: Path | None = None,
    orphaned_capture_sha256: str | None = None,
    orphaned_preflight: Path | None = None,
    orphaned_preflight_sha256: str | None = None,
    prepare_only: bool = False,
    lock_root: Path | None = None,
    native_system_loss: bool = False,
    pre_mutation_failure: bool = False,
    pre_mutation_evidence: Path | None = None,
    pre_mutation_evidence_sha256: str | None = None,
) -> Path:
    """Archive one explicitly identified unresolved owner after independent ON evidence.

    Held owners additionally require the unchanged original capture and absence
    of its host process group; a dead parent alone cannot establish an orphan.
    First use prepare_only to persist the absence-check boundary without release,
    then collect independent ON evidence starting strictly after that boundary.

    Never adopt the old lease, alter its journal or claim the RF attempt passed.
    The root mutex coordinates with acquire; only the final same-root rename
    frees the device name. A verified intent without its matching archive is not
    a release receipt. Failure before that commit leaves the original lock held.
    No live device command, radio mutation or automatic retry occurs here.
    """
    _require(
        authorize_restoration_only is True,
        "explicit restoration-only authorization required",
    )
    _require(type(prepare_only) is bool, "prepare-only flag must be boolean")
    _require(
        type(pre_mutation_failure) is bool,
        "pre-mutation failure flag must be boolean",
    )
    if pre_mutation_failure:
        _require(
            original_status == "quarantined"
            and native_system_loss
            and not prepare_only
            and inspection_input is None
            and pre_mutation_evidence is not None
            and _hash(pre_mutation_evidence_sha256),
            "pre-mutation recovery requires a quarantined native owner and evidence",
        )
    else:
        _require(
            pre_mutation_evidence is None and pre_mutation_evidence_sha256 is None,
            "pre-mutation evidence requires explicit pre-mutation recovery",
        )
    _require(
        original_status in ("quarantined", "held"),
        "explicit original status must be quarantined or held",
    )
    if original_status == "held":
        if native_system_loss:
            _require(
                orphaned_capture is None and orphaned_capture_sha256 is None,
                "native system-loss recovery has no startup capture",
            )
            _require(
                orphaned_preflight is not None
                and (prepare_only is True or _hash(orphaned_preflight_sha256)),
                "native system-loss recovery requires a preflight path/hash",
            )
        else:
            _require(
                orphaned_capture is not None
                and _hash(orphaned_capture_sha256)
                and orphaned_preflight is not None
                and (prepare_only is True or _hash(orphaned_preflight_sha256)),
                "orphaned-held recovery requires original capture and preflight paths/hashes",
            )
        _require(
            not prepare_only or orphaned_preflight_sha256 is None,
            "preparation creates a new preflight, never replaces an existing proof",
        )
    elif not pre_mutation_failure:
        _require(
            orphaned_capture is None
            and orphaned_capture_sha256 is None
            and orphaned_preflight is None
            and orphaned_preflight_sha256 is None
            and prepare_only is False,
            "capture recovery arguments are only for orphaned-held owners",
        )
    device = canonical_physical_udid(device)
    _require(
        type(run_id) is str and str(uuid.UUID(run_id)) == run_id,
        "canonical original run required",
    )
    _require(
        _hash(owner_sha256) and _hash(journal_sha256),
        "explicit original quarantine hashes required",
    )
    _require(
        prepare_only or inspection_input is not None or pre_mutation_failure,
        "independent inspection input required for archival",
    )
    journal, output = (Path(p).absolute() for p in (journal, output))
    if inspection_input is not None:
        inspection_input = Path(inspection_input).absolute()
    pre_mutation_evidence_path: Path | None = None
    pre_mutation_evidence_raw: bytes | None = None
    pre_mutation_evidence_info: os.stat_result | None = None
    if pre_mutation_failure:
        assert pre_mutation_evidence is not None
        pre_mutation_evidence_path = Path(pre_mutation_evidence).absolute()
        _require(
            not pre_mutation_evidence_path.is_relative_to(output),
            "pre-mutation evidence overlaps restoration output",
        )
        pre_mutation_evidence_raw = _read_small(pre_mutation_evidence_path, 65536)
        pre_mutation_evidence_info = pre_mutation_evidence_path.lstat()
        _require(
            hashlib.sha256(pre_mutation_evidence_raw).hexdigest()
            == pre_mutation_evidence_sha256,
            "pre-mutation evidence hash mismatch",
        )
    lock_root = Path(lock_root or ROOT / ".work/ios-radio-leases").absolute()
    _require(
        not output.is_relative_to(lock_root) and not journal.is_relative_to(lock_root),
        "restoration evidence must be outside lock root",
    )
    if pre_mutation_evidence_path is not None:
        _require(
            not pre_mutation_evidence_path.is_relative_to(lock_root),
            "pre-mutation evidence must be outside lock root",
        )
    root = _directory(lock_root)
    directory = None
    try:
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root_identity = _inode(os.fstat(root))
        _require(os.fstat(root).st_uid == os.getuid(), "foreign phone lock root owner")
        name = hashlib.sha256(device.encode()).hexdigest()
        pending = name + ".restoration-pending"
        _require(
            not os.path.lexists(lock_root / pending),
            "an unresolved restoration commit already blocks this phone",
        )
        directory = os.open(name, _DIRECTORY_FLAGS, dir_fd=root)
        lock_info = os.fstat(directory)
        _require(lock_info.st_uid == os.getuid(), "foreign quarantined directory owner")
        lock_identity = _inode(lock_info)
        owner_path = lock_root / name / "owner.json"
        owner_raw = _read_small(owner_path, 65536)
        owner_identity = _inode(
            os.stat("owner.json", dir_fd=directory, follow_symlinks=False)
        )
        raw = _read_small(journal, 65536)
        journal_info = journal.lstat()
        journal_identity = _inode(journal_info)
        _require(
            hashlib.sha256(owner_raw).hexdigest() == owner_sha256
            and hashlib.sha256(raw).hexdigest() == journal_sha256,
            "original quarantine hash mismatch",
        )
        owner, document = (
            _recovery_json(owner_raw.decode()),
            _recovery_json(raw.decode()),
        )
        _require(
            set(owner) == {"device", "run_id", "host_pid", "scope", "owner_nonce"},
            "original owner exact schema",
        )
        _require(
            owner["device"] == document.get("device") == device
            and owner["run_id"] == document.get("run_id") == run_id
            and owner["scope"] == document.get("scope")
            and owner["scope"] in {SCOPE, NATIVE_SYSTEM_LOSS_SCOPE},
            "foreign quarantine run/device/scope",
        )
        _require(
            not native_system_loss or owner["scope"] == NATIVE_SYSTEM_LOSS_SCOPE,
            "native system-loss recovery scope mismatch",
        )
        pid = owner["host_pid"]
        _require(
            type(pid) is int and pid > 0 and pid == document.get("host_pid"),
            "original host PID mismatch",
        )
        _require(
            type(owner["owner_nonce"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", owner["owner_nonce"]) is not None,
            "original owner nonce required",
        )
        _require(
            type(document.get("schema_version")) is int
            and document["schema_version"] == 1
            and document.get("status") == original_status
            and document.get("restoration") == "unverified"
            and (not pre_mutation_failure or document.get("control_sha256") is None)
            and "released_monotonic" in document
            and document["released_monotonic"] is None
            and "error" in document
            and (
                document["error"] is None
                if original_status == "held"
                else type(document["error"]) is str and bool(document["error"])
            ),
            "only the explicitly selected unresolved original owner can be restored",
        )
        capture_raw = None
        capture_pid = None
        if original_status == "held" and not native_system_loss:
            orphaned_capture = Path(orphaned_capture).absolute()
            _require(
                not orphaned_capture.is_relative_to(lock_root)
                and not orphaned_capture.is_relative_to(output),
                "original capture must be outside lock root and recovery output",
            )
            capture_raw = _read_small(orphaned_capture, 65536)
            capture_info = orphaned_capture.lstat()
            _require(
                hashlib.sha256(capture_raw).hexdigest() == orphaned_capture_sha256,
                "original capture hash mismatch",
            )
            capture = _recovery_json(capture_raw.decode())
            capture_pid = capture.get("capture_pid")
            _require(
                type(capture.get("schema_version")) is int
                and capture["schema_version"] == 1
                and capture.get("scope") == "ios-adapter-startup-window-candidate"
                and capture.get("platform") == "ios"
                and capture.get("device") == device
                and capture.get("run_id") == run_id
                and capture.get("status") == "running"
                and type(capture_pid) is int
                and capture_pid > 0
                and capture_pid != pid,
                "original orphaned capture identity/status mismatch",
            )

        def guard() -> None:
            current = _directory(lock_root)
            try:
                _require(
                    _inode(os.fstat(current)) == root_identity, "phone lock root drift"
                )
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
                _require(
                    stat.S_ISDIR(info.st_mode) and _inode(info) == lock_identity,
                    "quarantine directory drift",
                )
            finally:
                os.close(current)
            _require(
                os.listdir(directory) == ["owner.json"], "foreign quarantine children"
            )
            IosRadioLease._check_file(
                directory, "owner.json", owner_identity, owner_raw
            )
            parent = _directory(journal.parent)
            try:
                IosRadioLease._check_file(parent, journal.name, journal_identity, raw)
                _require(
                    os.stat(
                        journal.name, dir_fd=parent, follow_symlinks=False
                    ).st_mtime_ns
                    == journal_info.st_mtime_ns,
                    "original quarantine journal timestamp drift",
                )
            finally:
                os.close(parent)
            if capture_raw is not None:
                parent = _directory(orphaned_capture.parent)
                try:
                    IosRadioLease._check_file(
                        parent, orphaned_capture.name, _inode(capture_info), capture_raw
                    )
                    _require(
                        os.stat(
                            orphaned_capture.name, dir_fd=parent, follow_symlinks=False
                        ).st_mtime_ns
                        == capture_info.st_mtime_ns,
                        "original capture timestamp drift",
                    )
                finally:
                    os.close(parent)
            checks = [(os.kill, pid, "owner")]
            if capture_pid is not None:
                # StartupWindowCapture uses start_new_session=True. Even after its
                # leader exits, surviving xcodebuild children keep the group busy.
                checks += [
                    (os.kill, capture_pid, "capture"),
                    (os.killpg, capture_pid, "capture process group"),
                ]
            for check, process, role in checks:
                try:
                    check(process, 0)
                except ProcessLookupError:
                    continue
                raise RuntimeError(
                    f"original {role} is still alive; no restoration takeover"
                )

        guard()
        preflight_raw = None
        if original_status == "held":
            orphaned_preflight = Path(orphaned_preflight).absolute()
            _require(
                not orphaned_preflight.is_relative_to(lock_root)
                and not orphaned_preflight.is_relative_to(output),
                "preflight must be outside lock root and inspection output",
            )
            binding = {
                "schema_version": 1,
                "scope": (
                    "ios-native-system-loss-orphaned-held-preflight-only"
                    if native_system_loss
                    else "ios-radio-orphaned-held-preflight-only"
                ),
                "device": device,
                "original_run_id": run_id,
                "owner_sha256": owner_sha256,
                "journal_sha256": journal_sha256,
                "capture_sha256": orphaned_capture_sha256,
                "host_pid": pid,
                "capture_pid": capture_pid,
            }
            if prepare_only:
                binding["checked_at"] = time.time()
                parent = _directory(orphaned_preflight.parent)
                try:
                    descriptor = os.open(
                        orphaned_preflight.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(
                            (json.dumps(binding, sort_keys=True) + "\n").encode()
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(parent)
                finally:
                    os.close(parent)
                return orphaned_preflight
            preflight_raw = _read_small(orphaned_preflight, 65536)
            preflight_info = orphaned_preflight.lstat()
            preflight = _recovery_json(preflight_raw.decode())
            checked_at = preflight.pop("checked_at", None)
            _require(
                hashlib.sha256(preflight_raw).hexdigest() == orphaned_preflight_sha256
                and preflight == binding
                and type(checked_at) in (int, float)
                and math.isfinite(checked_at)
                and checked_at <= preflight_info.st_mtime + 1,
                "original orphaned-held preflight hash/identity/time mismatch",
            )
        proof = None
        if not pre_mutation_failure:
            assert inspection_input is not None
            proof = collect_inspection(
                inspection_input,
                output,
                device=device,
                owner_sha256=owner_sha256,
                journal_sha256=journal_sha256,
            )
        if proof is not None:
            _require(
                proof.get("scope") == "ios-radio-restoration-inspection-only"
                and proof.get("device") == device
                and proof.get("owner_sha256") == owner_sha256
                and proof.get("original_journal_sha256") == journal_sha256
                and proof.get("inspected_switch_value") == "1",
                "inspection does not bind this quarantine",
            )
            start = proof.get("observed_start_time")
            finish = proof.get("observed_finish_time")
            _require(
                all(
                    type(t) in (int, float) and math.isfinite(t)
                    for t in (start, finish)
                )
                and max(
                    journal_info.st_mtime,
                    capture_info.st_mtime if capture_raw is not None else 0,
                    max(preflight_info.st_mtime, checked_at)
                    if preflight_raw is not None
                    else 0,
                )
                < start
                < finish
                and -5 <= time.time() - finish <= INSPECTION_MAX_AGE_SECONDS,
                "inspection must be fresh and later than original owner/preflight",
            )
            guard()
            verify_inspection(proof, output)
        else:
            _require(
                pre_mutation_failure
                and pre_mutation_evidence_path is not None
                and pre_mutation_evidence_raw is not None
                and pre_mutation_evidence_info is not None,
                "pre-mutation archive evidence is missing",
            )
            _require(
                hashlib.sha256(pre_mutation_evidence_raw).hexdigest()
                == pre_mutation_evidence_sha256,
                "pre-mutation archive evidence changed",
            )
            guard()
        archive = lock_root / ("restored-" + name + "-" + uuid.uuid4().hex)
        receipt = {
            "schema_version": 1,
            "scope": (
                "ios-native-system-loss-pre-mutation-restoration-only"
                if pre_mutation_failure
                else (
                    (
                        "ios-native-system-loss-orphaned-held-restoration-only"
                        if original_status == "held"
                        else "ios-native-system-loss-restoration-only"
                    )
                    if native_system_loss
                    else (
                        "ios-radio-orphaned-held-restoration-only"
                        if original_status == "held"
                        else "ios-radio-quarantine-restoration-only"
                    )
                )
            ),
            "status": (
                "verified-pre-mutation-awaiting-archive-commit"
                if pre_mutation_failure
                else "verified-on-awaiting-archive-commit"
            ),
            "device": device,
            "original_run_id": run_id,
            "original_owner": owner,
            "original_journal": str(journal),
            "original_owner_sha256": owner_sha256,
            "original_journal_sha256": journal_sha256,
            "original_journal_status": original_status,
            "original_attempt_cleanup_proven": False,
            "original_attempt_verdict_unchanged": True,
            "archive_path": str(archive),
            "archive_identity": list(lock_identity),
            "commit_rule": "release only if archive directory has the recorded inode and unchanged owner bytes",
            "inspection": proof,
            "rf_gate_passed": False,
        }
        if native_system_loss:
            receipt["native_system_loss_recovery"] = {
                "owner_pid_absent": True,
                "rf_gate_passed": False,
                "manual_or_automated_radio_restoration": (
                    "not-required-before-mutation"
                    if pre_mutation_failure
                    else "inspection-bound"
                ),
            }
        if pre_mutation_evidence_raw is not None:
            receipt["pre_mutation_evidence"] = {
                "path": str(pre_mutation_evidence_path),
                "sha256": pre_mutation_evidence_sha256,
                "radio_mutation_proven": False,
            }
        if capture_raw is not None:
            receipt["original_capture"] = {
                "path": str(orphaned_capture),
                "sha256": orphaned_capture_sha256,
                "pid": capture_pid,
                "process_group_absent": True,
            }
            receipt["orphaned_preflight"] = {
                "path": str(orphaned_preflight),
                "sha256": orphaned_preflight_sha256,
                "checked_at": checked_at,
            }
        added_files = {
            "original-owner.json": owner_raw,
            "original-phone-journal.json": raw,
            "restoration-release.json": (
                json.dumps(receipt, sort_keys=True) + "\n"
            ).encode(),
        }
        if capture_raw is not None:
            added_files["original-capture.json"] = capture_raw
        if preflight_raw is not None:
            added_files["orphaned-held-preflight.json"] = preflight_raw
        if pre_mutation_evidence_raw is not None:
            added_files["original-pre-mutation-evidence"] = pre_mutation_evidence_raw
        directories = {output}
        if proof is not None:
            for row in proof["files"]:
                path = output / row["path"]
                with _open_regular(path) as stream:
                    os.fsync(stream.fileno())
                directories.update(
                    parent
                    for parent in path.parents
                    if parent == output or parent.is_relative_to(output)
                )
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            descriptor = _directory(path)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        out = _directory(output)
        try:
            for filename, content in added_files.items():
                descriptor = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=out,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.fsync(out)
        finally:
            os.close(out)
        guard()
        pending_raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root,
        )
        with os.fdopen(descriptor, "wb") as stream:
            pending_identity = _inode(os.fstat(stream.fileno()))
            stream.write(pending_raw)
            stream.flush()
            os.fsync(stream.fileno())
        # This durable blocker survives even a failed archival rollback. acquire
        # never clears it, and ordinary runs cannot adopt an interrupted recovery.
        os.fsync(root)
        if proof is not None:
            verify_inspection(proof, output, added_files=added_files)
        guard()
        if pre_mutation_evidence_path is not None:
            parent = _directory(pre_mutation_evidence_path.parent)
            try:
                IosRadioLease._check_file(
                    parent,
                    pre_mutation_evidence_path.name,
                    _inode(pre_mutation_evidence_info),
                    pre_mutation_evidence_raw,
                )
            finally:
                os.close(parent)
        if preflight_raw is not None:
            parent = _directory(orphaned_preflight.parent)
            try:
                IosRadioLease._check_file(
                    parent,
                    orphaned_preflight.name,
                    _inode(preflight_info),
                    preflight_raw,
                )
                _require(
                    os.stat(
                        orphaned_preflight.name, dir_fd=parent, follow_symlinks=False
                    ).st_mtime_ns
                    == preflight_info.st_mtime_ns,
                    "orphaned-held preflight timestamp drift",
                )
            finally:
                os.close(parent)
        if proof is not None:
            _require(
                -5 <= time.time() - finish <= INSPECTION_MAX_AGE_SECONDS,
                "inspection expired before archival commit",
            )
        _require(
            not archive.exists() and not archive.is_symlink(),
            "restoration archive already exists",
        )
        os.rename(name, archive.name, src_dir_fd=root, dst_dir_fd=root)
        try:
            os.fsync(root)
            IosRadioLease._check_file(root, pending, pending_identity, pending_raw)
            os.unlink(pending, dir_fd=root)
            # No later fallible action precedes unlock. A crash may conservatively
            # resurrect this blocker, but cannot expose an uncommitted release.
        except (OSError, RuntimeError) as error:
            # acquire uses the same mutex, so no cooperating new owner can enter
            # while an unsuccessful durable commit is rolled back.
            try:
                info = os.stat(archive.name, dir_fd=root, follow_symlinks=False)
                _require(
                    _inode(info) == lock_identity,
                    "restoration archive drift during rollback",
                )
                _require(
                    not os.path.lexists(lock_root / name),
                    "foreign owner blocks archival rollback",
                )
                os.rename(archive.name, name, src_dir_fd=root, dst_dir_fd=root)
                os.fsync(root)
            except (OSError, RuntimeError) as rollback:
                error.add_note(f"restoration archive rollback: {rollback}")
            raise
        return output / "restoration-release.json"
    finally:
        if directory is not None:
            os.close(directory)
        os.close(root)


def _owner_hint(root: int, name: str) -> str:
    """Describe the lease that already owns this device name, best-effort.

    Read-only and additive: a missing or unreadable owner record must not change the
    refusal, only make it less specific than it could have been.
    """
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root)
    except OSError:
        return ""
    try:
        owner = os.open("owner.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            document = json.loads(os.read(owner, 65536).decode("utf-8"))
        finally:
            os.close(owner)
    except (OSError, ValueError):
        return f"; its lock directory is {name!r} under the phone lock root"
    finally:
        os.close(descriptor)
    if type(document) is not dict:
        return f"; its lock directory is {name!r} under the phone lock root"
    return (
        f" by run {document.get('run_id')!r} (scope {document.get('scope')!r}, "
        f"host pid {document.get('host_pid')!r}); its journal records the status. "
        "Never adopted or cleared automatically"
    )


def archive_observation_owner(
    device: str,
    *,
    journal: Path,
    output: Path,
    owner_sha256: str,
    journal_sha256: str,
    authorize_archive: bool = False,
    lock_root: Path | None = None,
) -> Path:
    """Archive one quarantined App-observation lease so the device can be used again.

    A quarantined observation lease is terminal: its journal may not be rewritten and its
    lock is never adopted, which is why the next run for this device is refused. For the
    radio scopes that resolution is `archive_phone_owner`, which demands independent
    radio-ON inspection. An observation lease never touches the radio at all — that is
    what the scope means, and the class exposes no radio operation — so demanding a radio
    inspection would be asking for evidence of a thing that could not have happened.

    This is the narrower equivalent for that scope. It proves: the journal is this
    device's quarantined observation lease with unchanged bytes; the lock belongs to that
    same run with unchanged owner bytes; the recorded host process is gone; no pending
    restoration blocker exists; no cooperating owner holds the mutex. Then it records a
    receipt outside the lock and commits one same-root rename to
    `restored-<name>-<nonce>`, verifying the archived inode afterwards.

    It never adopts the lease, never rewrites the journal, never claims RF or
    resource-zero, and never deletes anything.
    """
    _require(
        authorize_archive is True,
        "explicit archive authorization required for a quarantined observation lease",
    )
    _require(_hash(owner_sha256) and _hash(journal_sha256), "recorded hashes required")
    canonical = canonical_physical_udid(device)
    root_path = Path(lock_root or ROOT / ".work/ios-radio-leases").absolute()
    name = hashlib.sha256(canonical.encode()).hexdigest()

    journal_raw = _read_small(journal, 65536)
    _require(
        hashlib.sha256(journal_raw).hexdigest() == journal_sha256,
        "observation journal changed since it was quarantined",
    )
    document = _recovery_json(journal_raw.decode("utf-8"))
    _require(
        document.get("scope") in OBSERVATION_SCOPES
        and document.get("status") in {"quarantined", "held"}
        and document.get("device") == canonical
        and document.get("platform") == "ios",
        "journal is not this device's quarantined observation lease",
    )
    if document.get("status") == "held":
        # An interrupted host leaves `held`: no terminal journal was published. That is
        # archivable only because an observation scope cannot mutate the radio by
        # construction, so it must also prove it recorded no mutation or restoration.
        _require(
            document.get("control_sha256") is None
            and document.get("restoration") == "unverified",
            "held observation lease records radio evidence; use the radio archive path",
        )
    run_id = document.get("run_id")
    _require(
        type(run_id) is str and str(uuid.UUID(run_id)) == run_id,
        "journal has no canonical run identity",
    )
    host_pid = document.get("host_pid")
    _require(type(host_pid) is int and host_pid > 0, "journal has no host process")
    if host_pid != os.getpid():
        try:
            os.kill(host_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise RuntimeError(
                "the recorded observation host is still alive; it would still own this device"
            ) from None
        else:
            raise RuntimeError(
                f"the recorded observation host pid {host_pid} is still alive; refusing to "
                "archive a lease whose owner could still act"
            )

    root = _directory(root_path)
    try:
        pending = name + ".restoration-pending"
        _require(
            not os.path.lexists(root_path / pending),
            "pending restoration blocks archival of this device",
        )
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = os.open(name, _DIRECTORY_FLAGS, dir_fd=root)
        try:
            lock_identity = _inode(os.fstat(directory))
            owner = os.open("owner.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                owner_raw = os.read(owner, 65536)
            finally:
                os.close(owner)
        finally:
            os.close(directory)
        _require(
            hashlib.sha256(owner_raw).hexdigest() == owner_sha256,
            "observation owner record changed since it was recorded",
        )
        owner = _recovery_json(owner_raw.decode("utf-8"))
        _require(
            owner.get("device") == canonical
            and owner.get("run_id") == run_id
            and owner.get("scope") in OBSERVATION_SCOPES,
            "lock belongs to a different device/run/scope than the journal",
        )
        archive = root_path / ("restored-" + name + "-" + uuid.uuid4().hex)
        _require(not os.path.lexists(archive), "archival target already exists")
        receipt = {
            "schema_version": 1,
            "scope": "ios-observation-quarantine-archive",
            "status": "verified-awaiting-archive-commit",
            "device": canonical,
            "original_run_id": run_id,
            "original_journal": str(journal),
            "original_journal_sha256": journal_sha256,
            "original_owner_sha256": owner_sha256,
            "host_pid_absent": True,
            "radio_state": "never-owned-by-this-scope",
            "rf_gate_passed": False,
            "archive_path": str(archive),
        }
        output = output.absolute()
        output.mkdir(parents=True, mode=0o700)
        _require(
            not (output / "archive.json").exists(), "archive receipt already exists"
        )
        (output / "archive.json").write_text(
            json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        (output / "original-owner.json").write_bytes(owner_raw)
        (output / "original-phone-journal.json").write_bytes(journal_raw)
        os.rename(name, archive.name, src_dir_fd=root, dst_dir_fd=root)
        info = os.stat(archive.name, dir_fd=root, follow_symlinks=False)
        _require(
            _inode(info) == lock_identity and not os.path.lexists(root_path / name),
            "archival did not move this exact lock",
        )
        os.fsync(root)
        return output / "archive.json"
    finally:
        os.close(root)


def _archive_observation(args: argparse.Namespace) -> int:
    """The `archive-observation` subcommand: a quarantined App-observation lease."""
    receipt = archive_observation_owner(
        args.device,
        journal=args.journal,
        output=args.output,
        owner_sha256=args.owner_sha256,
        journal_sha256=args.journal_sha256,
        authorize_archive=args.authorize_archive,
    )
    print(f"ios-observation-archive:archived receipt={receipt} scope=observation-only")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Explicit archival of one quarantined iOS phone owner; no radio or RF commands. "
            "Use the archive-observation subcommand for a quarantined formal-scan lease."
        )
    )
    parser.add_argument(
        "command", nargs="?", choices=("archive-observation",), default=None
    )
    parser.add_argument("device", nargs="?")
    parser.add_argument(
        "--run-id",
        help="required for a radio-scope archive, unused by archive-observation",
    )
    parser.add_argument(
        "--original-status",
        choices=("quarantined", "held"),
        help="required for a radio-scope archive, unused by archive-observation",
    )
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--orphaned-capture", type=Path)
    parser.add_argument("--orphaned-capture-sha256")
    parser.add_argument("--orphaned-preflight", type=Path)
    parser.add_argument("--orphaned-preflight-sha256")
    parser.add_argument("--prepare-orphaned-held", action="store_true")
    parser.add_argument(
        "--native-system-loss",
        action="store_true",
        help="Recover an interrupted native BLeng system-loss owner without RN startup capture",
    )
    parser.add_argument("--inspection-input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner-sha256", required=True)
    parser.add_argument("--journal-sha256", required=True)
    parser.add_argument("--authorize-restoration-only", action="store_true")
    parser.add_argument(
        "--pre-mutation-failure",
        action="store_true",
        help="Archive a quarantined native owner proven to fail before radio mutation",
    )
    parser.add_argument("--pre-mutation-evidence", type=Path)
    parser.add_argument("--pre-mutation-evidence-sha256")
    parser.add_argument(
        "--authorize-archive",
        action="store_true",
        help=(
            "authorize archiving a quarantined App-observation lease; that scope never "
            "owns the radio, so no radio inspection applies"
        ),
    )
    args = parser.parse_args()
    if args.command == "archive-observation":
        return _archive_observation(args)
    if args.device is None:
        parser.error("a device argument is required")
    for flag in ("run_id", "original_status"):
        if getattr(args, flag) is None:
            parser.error(f"--{flag.replace('_', '-')} is required for a radio archive")
    receipt = archive_phone_owner(
        args.device,
        args.run_id,
        original_status=args.original_status,
        journal=args.journal,
        orphaned_capture=args.orphaned_capture,
        orphaned_capture_sha256=args.orphaned_capture_sha256,
        orphaned_preflight=args.orphaned_preflight,
        orphaned_preflight_sha256=args.orphaned_preflight_sha256,
        prepare_only=args.prepare_orphaned_held,
        inspection_input=args.inspection_input,
        output=args.output,
        owner_sha256=args.owner_sha256,
        journal_sha256=args.journal_sha256,
        authorize_restoration_only=args.authorize_restoration_only,
        native_system_loss=args.native_system_loss,
        pre_mutation_failure=args.pre_mutation_failure,
        pre_mutation_evidence=args.pre_mutation_evidence,
        pre_mutation_evidence_sha256=args.pre_mutation_evidence_sha256,
    )
    action = (
        "preflight-only-owner-still-held" if args.prepare_orphaned_held else "archived"
    )
    print(
        f"ios-radio-restoration:{action} receipt={receipt} scope=restoration-only-not-RF"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
