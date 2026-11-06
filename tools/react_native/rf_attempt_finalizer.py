"""Shared RF attempt finalization; runners retain signals and fixture ownership."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.react_native.rf_evidence_bundle import EvidenceBundle


def finalize_rf_attempt(
    *,
    coordination: Path,
    bundle: EvidenceBundle | None,
    completed: bool,
    primary: BaseException | None,
    runtime_log: Path,
    stop_owned: Callable[[], None] | None,
    report: Callable[[bool], None],
) -> None:
    """Finish once after owned cleanup, annotating rather than replacing primary.

    The runner binds stop_owned to its original fixture identity, or passes None
    without ownership. completed records the independent scenario check, not
    cleanup success. Signal masking/restoration stays with the calling runner.
    """
    errors: list[str] = []
    if primary is not None:
        try:
            (coordination / "abort").write_text("parent-failed\n", encoding="utf-8")
        except OSError as error:
            errors.append(f"abort publication: {error}")
    if stop_owned is not None:
        try:
            stop_owned()
        except (
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            errors.append(f"fixture cleanup: {error}")
    if bundle is not None:
        try:
            bundle.finish(
                completed=completed,
                primary_error=primary,
                cleanup_errors=errors,
                runtime_log=runtime_log,
            )
        except Exception as error:  # noqa: BLE001 - never replace the primary RF failure
            errors.append(f"bundle finalization: {error}")
    report(completed and not errors)
    if errors:
        message = "; ".join(errors)
        if primary is not None:
            primary.add_note(message)
        else:
            raise RuntimeError(message)
