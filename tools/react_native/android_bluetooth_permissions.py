"""Owner-local formal Android Bluetooth permission preparation and restoration.

This scope owns only the selected package's user0 runtime permissions. It does
not own a phone lease, radio state, application lifecycle, or process cleanup;
``phone_guard`` must be the caller's existing lease check. Every device command
is journaled by :class:`AndroidPermissionController`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from tools.android.permission_control import (
    PERMISSIONS,
    AndroidPermissionController,
)


class AndroidBluetoothPermissionScope:
    """Temporarily grant formal-package Bluetooth permissions under one lease.

    ``prep`` (also available as ``enter`` and through the context-manager API)
    captures the exact baseline grants for ``package``, grants only missing
    permissions with root, and requires an exact all-granted readback. ``finish``
    restores that exact baseline. A failed action is sticky; failed restoration
    or evidence publication marks the scope ``quarantined`` and is never reported
    as a pass. This class never changes the radio, launches an app, or kills a
    process.
    """

    def __init__(
        self,
        device: str,
        run_id: str,
        evidence_path: Path,
        package: str,
        phone_guard: Callable[[], None],
        use_root: bool = True,
        command_runner: Callable[..., Any] | None = None,
    ) -> None:
        if use_root is not True:
            raise ValueError("formal Android permission scope requires explicit root")
        self.controller = AndroidPermissionController(
            device,
            run_id,
            evidence_path,
            phone_guard=phone_guard,
            use_root=use_root,
            package=package,
            command_runner=command_runner,
        )
        self.device = device
        self.run_id = run_id
        self.evidence_path = evidence_path
        self.package = package
        self.phone_guard = phone_guard
        self.baseline: dict[str, bool] | None = None
        self.prepared_grants: dict[str, bool] | None = None
        self.restored_grants: dict[str, bool] | None = None
        self.sticky_error: BaseException | None = None
        self.cleanup_errors: list[str] = []
        self.prepared = False
        self.finished = False
        self.controller.document.update(
            scope="android-bluetooth-permissions",
            baseline_grants=None,
            prepared_grants=None,
            restored_grants=None,
        )

    @property
    def uid(self) -> int | None:
        """The exact package UID read from the selected package dump."""
        return self.controller.uid

    @property
    def document(self) -> dict[str, Any]:
        """The live evidence document; command receipts are under ``actions``."""
        return self.controller.document

    def _remember(self, error: BaseException) -> BaseException:
        if self.sticky_error is None:
            self.sticky_error = error
        return self.sticky_error

    def _publish(self) -> None:
        try:
            self.controller._publish()
        except BaseException as error:
            self.cleanup_errors.append(f"permission scope publication: {error}")
            self.document["status"] = "quarantined"
            remembered = self._remember(error)
            if remembered is not error:
                remembered.add_note(f"permission scope publication: {error}")
            raise

    def _primary_failure(self, error: BaseException, *, publish: bool = True) -> None:
        remembered = self._remember(error)
        if self.document["primary_error"] is None:
            self.document["primary_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        self.document["status"] = "failed"
        if remembered is not error:
            error = remembered
        if publish:
            try:
                self.controller._publish()
            except BaseException as publication:  # noqa: BLE001
                self.cleanup_errors.append(
                    f"permission scope publication: {publication}"
                )
                self.document["status"] = "quarantined"
                error.add_note(f"permission scope publication: {publication}")

    def _cleanup_failure(self, error: BaseException) -> None:
        remembered = self._remember(error)
        self.cleanup_errors.append(f"permission restoration: {error}")
        if self.document["primary_error"] is None:
            self.document["primary_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        self.document["status"] = "quarantined"
        if remembered is not error:
            remembered.add_note(f"permission restoration: {error}")

    def _ready(self) -> None:
        if self.finished:
            raise RuntimeError("Android Bluetooth permission scope already finished")
        if self.sticky_error is not None:
            raise self.sticky_error
        if self.prepared:
            raise RuntimeError("Android Bluetooth permission scope already prepared")

    def prep(self) -> dict[str, bool]:
        """Capture baseline, grant missing permissions, and verify both grants."""
        self._ready()
        # This receipt is the platform evidence directory's first writer in the in-process
        # lane (which has no automation owner to create it), so it creates its own parent.
        self.evidence_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.evidence_path.exists() or self.evidence_path.is_symlink():
            error = RuntimeError("refusing existing permission-scope evidence")
            self._primary_failure(error, publish=False)
            raise error
        try:
            # Root proof is explicit and is never replaced by shell fallback.
            self.controller.require_root()
            baseline = self.controller.read_permissions(label="baseline")
            self.baseline = dict(baseline)
            self.document["baseline_grants"] = dict(baseline)
            prepared = self.controller.set_permissions(
                {permission: True for permission in PERMISSIONS},
                label="grant-missing",
            )
            if prepared != {permission: True for permission in PERMISSIONS}:
                raise RuntimeError(
                    "formal Bluetooth permission readback was not all true"
                )
            self.prepared_grants = dict(prepared)
            self.document["prepared_grants"] = dict(prepared)
            self.prepared = True
            self._publish()
            return dict(baseline)
        except BaseException as error:
            self._primary_failure(error)
            raise

    enter = prep

    def finish(self, primary_error: BaseException | None = None) -> list[str]:
        """Restore the exact baseline and publish a passed/failed/quarantined receipt."""
        if self.finished:
            return ["Android Bluetooth permission scope already finished"]
        self.finished = True
        errors: list[str] = []
        if primary_error is not None:
            self._primary_failure(primary_error)
        elif self.sticky_error is not None:
            self.document["primary_error"] = {
                "type": type(self.sticky_error).__name__,
                "message": str(self.sticky_error),
            }
            self.document["status"] = "failed"

        if self.baseline is None:
            if self.sticky_error is None:
                error = RuntimeError("permission baseline was never established")
                self._cleanup_failure(error)
            errors.extend(self.cleanup_errors)
        else:
            try:
                restored = self.controller.set_permissions(
                    self.baseline, label="restore-baseline"
                )
                if restored != self.baseline:
                    raise RuntimeError("permission baseline restore readback mismatch")
                self.restored_grants = dict(restored)
                self.document["restored_grants"] = dict(restored)
            except BaseException as error:  # noqa: BLE001 - restoration is quarantining
                self._cleanup_failure(error)
            errors.extend(self.cleanup_errors)

        if self.document["status"] != "quarantined":
            self.document["status"] = (
                "passed"
                if primary_error is None and self.sticky_error is None and not errors
                else "failed"
            )
        self.document["cleanup_errors"] = list(errors)
        try:
            self.controller._publish()
        except BaseException as publication:  # noqa: BLE001 - journal uncertainty
            errors.append(f"permission scope publication: {publication}")
            self.document["cleanup_errors"] = list(errors)
            self.document["status"] = "quarantined"
            if primary_error is not None:
                primary_error.add_note(f"permission scope publication: {publication}")
            else:
                self._remember(publication)
        if primary_error is not None:
            for error in errors:
                primary_error.add_note(error)
        return errors

    restore = finish
    close = finish

    def __enter__(self) -> Self:
        self.prep()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> bool:
        errors = self.finish(exc_value)
        if exc_value is None and errors:
            raise RuntimeError("; ".join(errors))
        return False
