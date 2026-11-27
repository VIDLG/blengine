"""Run the managed formal Android scenes in order, as one sequence.

This entry binds the shared sequence engine (`scene_sequence`) to the Android lane. Android
needs nothing before a scene is admitted: its producer refuses to take over a running App,
replaces the installation itself and always stops the App it launched, so every scene starts
from a device with nothing of ours running — or refuses to start.

It runs every managed scene in the shared dependency order (`scene_sequence.ORDER`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import scene_sequence
from .bleng_android_scan import MINIMUM_SCENARIO_SECONDS
from .run_bleng_android_scan import main as run_scene
from .scene_sequence import SceneOutcome, SceneSequenceError

SEQUENCE = scene_sequence.ORDER


def run_sequence(
    *,
    output: Path,
    arguments: Sequence[str],
    scenes: Sequence[str] = SEQUENCE,
    run: Callable[[list[str]], int] = run_scene,
) -> list[SceneOutcome]:
    """Run each scene once; the producer's own absence check is the only admission."""
    return scene_sequence.run_sequence(
        output=output,
        arguments=arguments,
        order=scenes,
        run=run,
        preparation_seconds=MINIMUM_SCENARIO_SECONDS,
    )


def main(argv: list[str] | None = None) -> int:
    output, arguments = scene_sequence.parse(argv, description=__doc__)
    scene_sequence.create_root(output)
    outcomes = run_sequence(output=output, arguments=arguments)
    return scene_sequence.report(outcomes, SEQUENCE, output)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, SceneSequenceError) as error:
        print(f"scene_sequence=failed error={error}", file=sys.stderr)
        raise SystemExit(2) from None
