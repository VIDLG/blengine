"""Run the managed formal iOS scenes in order, as one sequence.

This entry binds the shared sequence engine (`scene_sequence`) to the iOS lane: the one
thing iOS needs before a scene is admitted.
`--release-retained-app` is always applied, because a passing scene keeps the App it
launched as the evidence a human inspects, and the next scene would then be admitted on a
device that still runs it.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from . import scene_sequence
from .bleng_ios_scan import MINIMUM_SCENARIO_SECONDS
from .ios_app_cleanup import DEFAULT_BUNDLE, release_app
from .run_bleng_ios_scan import main as run_scene
from .scene_sequence import SceneOutcome, SceneSequenceError

__all__ = [
    "CLEANUP_SECONDS",
    "RELEASE_FLAG",
    "SEQUENCE",
    "SceneSequenceError",
    "_bundle_id",
    "_device",
    "main",
    "release_always",
    "run_sequence",
    "sequence_arguments",
]

# Every managed scene in the shared dependency order (`scene_sequence.ORDER`). The four
# fault-free scenes used to be the whole sequence; the fault-armed and repeating scenes are
# managed scenes of this lane too, and a platform is proven by all of them or not at all.
SEQUENCE = scene_sequence.ORDER

RELEASE_FLAG = "--release-retained-app"
# The platform restores the App it terminated as the last foreground one, and it can do so
# between the pre-admission release and the install — observed as `formal iOS App already
# running; refusing takeover` on the scene that followed a fast one. This flag is the same
# safe act one step later: the candidate terminates only the PID its own inventory
# attributed to this bundle, records the termination, and proves it took effect before
# installing. A foreign App still refuses takeover.
RECLAIM_FLAG = "--release-before-install"
# One bounded pre-admission cleanup per scene: two process inventories and at most one
# termination of this bundle's own installed App.
CLEANUP_SECONDS = 90.0


def _device(arguments: Sequence[str]) -> str:
    """The device the caller named once; the per-scene release cannot guess it."""
    device = scene_sequence.flag_value(arguments, "--device")
    if device is None:
        raise SceneSequenceError("the sequence requires an explicit --device")
    return device


def _bundle_id(arguments: Sequence[str]) -> str:
    return scene_sequence.flag_value(arguments, "--app-id") or DEFAULT_BUNDLE


def release_always(arguments: Sequence[str]) -> list[str]:
    """State the sequence's intent: no scene may keep, or meet, an App it did not launch."""
    per_scene = list(arguments)
    for flag in (RELEASE_FLAG, RECLAIM_FLAG):
        if flag not in per_scene:
            per_scene.append(flag)
    return per_scene


def sequence_arguments(
    arguments: Sequence[str], scene: str, output: Path, *, timeout: float | None = None
) -> list[str]:
    return scene_sequence.sequence_arguments(
        release_always(arguments), scene, output, timeout=timeout
    )


def run_sequence(
    *,
    output: Path,
    arguments: Sequence[str],
    device: str,
    bundle_id: str,
    scenes: Sequence[str] = SEQUENCE,
    run: Callable[[list[str]], int] = run_scene,
    release: Callable[..., object] = release_app,
) -> list[SceneOutcome]:
    """Run each scene once after releasing this bundle's own retained App."""

    def before_scene(scene: str, root: Path) -> None:
        release(  # type: ignore[operator]
            device=device,
            bundle_id=bundle_id,
            output=root / "release",
            deadline=time.monotonic() + CLEANUP_SECONDS,
        )

    return scene_sequence.run_sequence(
        output=output,
        arguments=release_always(arguments),
        order=scenes,
        run=run,
        before_scene=before_scene,
        # The scenario deadline is also the deadline cleanup runs under (`finalize`), so
        # cleanup has to be reserved *inside* it. Without the reserve a scene that used its
        # whole budget left cleanup a few seconds, and devicectl refuses any `--timeout`
        # below five — a passed scene then failed at `candidate.finalize`.
        preparation_seconds=MINIMUM_SCENARIO_SECONDS + CLEANUP_SECONDS,
    )


def main(argv: list[str] | None = None) -> int:
    output, arguments = scene_sequence.parse(argv, description=__doc__)
    device = _device(arguments)
    scene_sequence.create_root(output)
    scenes = run_sequence(
        output=output,
        arguments=arguments,
        device=device,
        bundle_id=_bundle_id(arguments),
    )
    return scene_sequence.report(scenes, SEQUENCE, output)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, SceneSequenceError) as error:
        print(f"scene_sequence=failed error={error}", file=sys.stderr)
        raise SystemExit(2) from None
